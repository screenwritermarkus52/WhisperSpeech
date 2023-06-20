# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/B2. Training (Lightning).ipynb.

# %% auto 0
__all__ = []

# %% ../nbs/B2. Training (Lightning).ipynb 2
import io
import time
import random
from pathlib import Path

from fastprogress import progress_bar, master_bar
import fastprogress
import wandb

import numpy as np
import pylab as plt

import IPython

import torch
import torch.nn as nn
from torch.utils.data.dataloader import DataLoader
from torch.profiler import record_function

# %% ../nbs/B2. Training (Lightning).ipynb 3
import lightning.pytorch as pl
import math

class TrainingTask(pl.LightningModule):
    def __init__(self, model, model_hparams=None):
        super().__init__()
        self.model = model
        self.model_hparams = model_hparams
    
    def configure_optimizers(self):
        """ Initialize AdamW optimizer"""
        all_params = set(self.model.parameters())
        wd_params = set()
        for m in self.model.modules():
            if isinstance(m, (nn.Linear, nn.Conv1d)):
                wd_params.add(m.weight)
                if m.bias is not None:
                    wd_params.add(m.bias)
        no_wd_params = all_params - wd_params

        optimizer = torch.optim.AdamW(lr=self.model_hparams['lr0'], betas=(0.9, 0.95),
            params=[
                {"params": list(wd_params), "weight_decay": self.model_hparams['weight_decay']},
                {"params": list(no_wd_params), "weight_decay": 0.0},
            ]
        )
        
        # modified from https://github.com/Lightning-AI/lightning/issues/5449#issuecomment-1501597319
        def num_steps_per_epoch() -> int:
            """Get number of steps"""
            # Accessing _data_source is flaky and might break
            dataset = self.trainer.fit_loop._data_source.dataloader()
            dataset_size = len(dataset)
            num_devices = max(1, self.trainer.num_devices)
            # math.ceil so always overestimate (underestimating throws exceptions)
            num_steps = math.ceil(dataset_size / (self.trainer.accumulate_grad_batches * num_devices))
            return num_steps
        
        total_steps = self.model_hparams['epochs'] * num_steps_per_epoch()
        self.model_hparams['pct_start'] = min(0.3, self.model_hparams['warmup_steps'] / total_steps)

        lr_scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer,
            pct_start=self.model_hparams['pct_start'],
            max_lr=self.model_hparams['lr0'],
            steps_per_epoch=num_steps_per_epoch(),
            epochs=self.model_hparams['epochs'],
            final_div_factor=25
        )

        return [optimizer], [{'scheduler': lr_scheduler, 'interval': 'step'}]
    
    def training_step(self, train_batch, batch_idx):
        x, y = train_batch
        train_logits, train_loss = self.model.forward(x, y)

        self.log("train_loss", train_loss, sync_dist=True)
        return train_loss
    
    def validation_step(self, val_batch, batch_idx):
        x, y = val_batch
        val_logits, val_loss = self.model.forward(x, y)

        self.log("val_loss", val_loss, sync_dist=True)
        return val_loss
    
    def test_step(self, val_batch, batch_idx):
        x, y = val_batch
        test_logits, test_loss = self.model.forward(x, y)

        self.log("test_loss", test_loss, sync_dist=True)
        return test_loss

# %% ../nbs/B2. Training (Lightning).ipynb 4
from fastcore.script import anno_parser
import shlex

# watch out: we can only pass Python values as keyword arguments (not positional)
# everything else has to be a string
def parse_and_call(name, fun, args, kwargs={}):
    p = anno_parser(fun)
    args = p.parse_args(args).__dict__
    args.pop('xtra'); args.pop('pdb')
    if type(wandb_logger.experiment.config) == wandb.sdk.wandb_config.Config:
        wandb_logger.experiment.config[name] = {k:v for k,v in args.items()}
    args.update({k:v for k, v in kwargs.items()})
    return fun(**args)

# %% ../nbs/B2. Training (Lightning).ipynb 6
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--task', type=str, help='Task to train')
parser.add_argument('--seed', type=int, default=0, help='Global training seed')
parser.add_argument('--batch-size', type=int, default=16, help='total batch size for all GPUs')
parser.add_argument('--workers', type=int, default=8, help='max dataloader workers (per RANK in DDP mode)')
parser.add_argument('--input-dir', type=str, default='', help='input data path') # fixed in the model for now
parser.add_argument("--checkpoint-dir", type=str, default="./checkpoints/", help="directory to save the checkpoints")
parser.add_argument('--epochs', type=int, default=10, help='total training epochs')
parser.add_argument('--weight-decay', type=float, default=1e-2, help='optimizer weight decay')
parser.add_argument('--lr0', type=float, default=1e-4, help='optimizer initial learning rate')
parser.add_argument('--clip-gradient-norm', type=float, default=None, help='enable gradient norm clipping')
parser.add_argument('--warmup-steps', type=int, default=10000, help='total number steps during which the learning rate rises (defaults to 10k updates)')

args = parser.parse_args().__dict__

task_args: list = shlex.split(args.pop("task"))
task_name, task_args = task_args[0], task_args[1:]
input_args: list = shlex.split(args.pop("input_dir"))
checkpoint_dir: str = args.pop("checkpoint_dir")
num_workers: int = args.pop("workers")
batch_size: int = args.pop("batch_size")
epochs: int = args.pop("epochs")

hyp_params = {}
hyp_params['warmup_steps'] = args['warmup_steps']
hyp_params['weight_decay'] = args['weight_decay']
hyp_params['clip_gradient_norm'] = args['clip_gradient_norm']
hyp_params['lr0'] = args['lr0']
hyp_params['epochs'] = epochs

# %% ../nbs/B2. Training (Lightning).ipynb 7
from lightning.pytorch.loggers import WandbLogger
from lightning.pytorch.callbacks import LearningRateMonitor
import importlib

torch.set_float32_matmul_precision('medium')

wandb_logger = WandbLogger(project=f"SpearTTS-{task_name}")
if type(wandb_logger.experiment.config) == wandb.sdk.wandb_config.Config:
    wandb_logger.experiment.config.update(hyp_params)

ckpt_callback = pl.callbacks.ModelCheckpoint(
     dirpath=f'{task_name}-{epochs}e',
     filename=task_name+"-{epoch}-{step}-{val_loss:.2f}",
     monitor="val_loss",
     save_top_k=4,
     every_n_epochs=1,
 )

lr_monitor_callback = LearningRateMonitor(logging_interval='step')

from torch.utils.data import DataLoader

task = importlib.import_module("spear_tts_pytorch."+task_name)

train_ds, val_ds = parse_and_call('dataset', task.load_datasets, input_args)

val_loader = DataLoader(val_ds,
    batch_size=batch_size,
    num_workers=num_workers,
    drop_last=False,
    pin_memory=True)

train_loader = DataLoader(train_ds,
    batch_size=batch_size,
    num_workers=num_workers,
    drop_last=False,
    shuffle=True,
    pin_memory=True)

model = parse_and_call('model', task.make_model, task_args, dict(dataset=train_ds))

task = TrainingTask(model, model_hparams=hyp_params)

trainer = pl.Trainer(max_epochs=hyp_params['epochs'],
                  accelerator="gpu",
                  profiler="simple",
                  precision='16-mixed',
                  gradient_clip_val=hyp_params['clip_gradient_norm'],
                  val_check_interval=1/10,
                  enable_checkpointing=True,
                  logger=wandb_logger,
                  callbacks=[ckpt_callback, lr_monitor_callback])

trainer.fit(model=task, train_dataloaders=train_loader, val_dataloaders=val_loader)
