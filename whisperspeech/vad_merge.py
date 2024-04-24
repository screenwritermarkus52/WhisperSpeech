# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/1C. VAD merging.ipynb.

# %% auto 0
__all__ = []

# %% ../nbs/1C. VAD merging.ipynb 2
import random

import numpy as np
import torch
import torch.nn.functional as F

from fastprogress import progress_bar
from fastcore.script import *

from . import utils
import webdataset as wds

# %% ../nbs/1C. VAD merging.ipynb 7
# we need to split first to merge in the spk_emb.npy data
# this is similar to utils.split_to_chunks but works without the audio data
def split(stream, ikey='vad.npy', copy_keys=[], split_keys=[]):
    empty = []
    for s in stream:
        imax = len(s[ikey]) - 1
        if len(s[ikey]) == 0:
            # Preserve info about audio files without any speech.
            # We need to push this info through a weird side-channel 
            # because we want to be able to a merge with naively
            # splitted data.
            new = {"__key__": s['__key__'] + "_none",
                   "src_key": s['__key__'],
                   "__url__": s['__url__']}
            for k in copy_keys:  new[k] = np.array([])
            for k in split_keys: new[k] = np.array([])
            new[ikey] = s[ikey]
            empty.append(new)
        for i,(ts,te) in enumerate(s[ikey]):
            new = {"__key__": s['__key__'] + f"_{i:03d}",
                   "src_key": s['__key__'],
                   "__url__": s['__url__'],
                   "i": i, "imax": imax,
                   "empty": empty}
            for k in copy_keys:  new[k] = s[k]
            for k in split_keys: new[k] = s[k][i]
            new[ikey] = s[ikey][i]
            yield new
            empty = []

def merge_by_src_key(stream, copy_keys=[], merge_keys=['vad.npy']):
    def make_record(src):
        s = {
            "__url__": src['__url__'],
            "__key__": src['src_key'],
        }
        for k in copy_keys: s[k] = src[k]
        for k in merge_keys: s[k] = []
        return s
    def finish_record(s):
        for k in merge_keys: s[k] = np.array(s[k])
        return s
    ms = None
    for s in stream:
        try:
            # push accumulated data
            if ms and s['src_key'] != ms['__key__']:
                yield finish_record(ms)
                ms = None
            # push all empty files we might have lost
            for vs in s.get("empty", []):
                yield finish_record(make_record(vs))
            # prepare a merged record for the new data
            if ms is None:
                ms = make_record(s)
            for k in merge_keys: ms[k].append(s[k])
        except:
            print(f"Error processing {s['__key__']}:")
            print(s)
            raise
    yield finish_record(ms)

# %% ../nbs/1C. VAD merging.ipynb 11
def random_cutter(dur):
    if random.random() < 0.5:
        return dur > 30 * (random.random()*0.95+0.05)
    else:
        return dur > 30

def random_cutter2(dur):
    if random.random() < 0.25:
        return True
    else:
        return dur > 30 * (random.random()*0.95+0.05)
    
def chunk_merger(prefix, should_cut=lambda x: x > 30):
    def _merger(stream):
        for s in stream:
            segments, speakers = s['vad.npy'], s['spk_emb.npy']
            if len(segments) == 0:
                s[prefix+'.vad.npy'], s[prefix+'.spk_emb.npy'] = np.array([]), np.array([])
                s[prefix+'.subvads.pyd'] = []
                yield s
                continue
            curr_start = segments[0][0]
            curr_end = 0
            curr_spk = None
            curr_chunks = []
            spk_acc = torch.tensor(speakers[0])
            spk_acc_N = 1
            merged = []
            merged_chunks = []
            merged_spk = []

            for (ts,te),new_spk in zip(segments, speakers):
                secs = te - ts
                new_spk = torch.tensor(new_spk)
                spk_change = False
                if curr_spk is not None:
                    sim = F.cosine_similarity(curr_spk, new_spk, dim=0)
                    spk_change = sim < 0.5 if secs > 2 else sim < 0.1
                if (spk_change or should_cut(te - curr_start)) and curr_end - curr_start > 0:
                    merged.append((curr_start, curr_end))
                    merged_spk.append(spk_acc / spk_acc_N)
                    merged_chunks.append(curr_chunks)
                    curr_start = ts
                    spk_acc = new_spk
                    curr_chunks = []
                curr_spk = new_spk
                if secs > 2:
                    spk_acc += new_spk
                    spk_acc_N += 1
                curr_end = te
                curr_chunks.append((ts, te))
            merged.append((curr_start, curr_end))
            merged_spk.append(spk_acc / spk_acc_N)
            merged_chunks.append(curr_chunks)
            s[prefix+'.vad.npy'], s[prefix+'.spk_emb.npy'] = np.array(merged), torch.stack(merged_spk).numpy()
            s[prefix+'.subvads.pyd'] = merged_chunks
            yield s
    return _merger

# %% ../nbs/1C. VAD merging.ipynb 17
# we filter before splitting to keep empty merged samples even if we filter out everything
def filter_bad_samples(stream):
    for s in stream:
        if 'librilight' in s['__url__'] or 'test-shard.tar' in s['__url__']:
            for k in ['vad.npy', 'spk_emb.npy', 'powers.npy']:
                s[k] = s[k][1:-1]

        if len(s['vad.npy']) > 0:
            lengths = s['vad.npy'][:,1] - s['vad.npy'][:,0]
            mask = (lengths < 1) & (s['powers.npy'] < -6)
            for k in ['vad.npy', 'spk_emb.npy', 'powers.npy']:
                s[k] = s[k][~mask]
        yield s
        

# %% ../nbs/1C. VAD merging.ipynb 19
@call_parse
def prepare_mvad(
    input:str,  # input VAD shard path
    output:str, # output shard path
    eqvad:bool=False, # make the chunk length distribution more uniform
    ignore_spk_emb:bool=False,
):    
    if ignore_spk_emb:
        def chg_spk_emb(stream):
            for s in stream:
                for x in s['spk_emb.npy']: x[:] = 1
                yield s
    else:
        def chg_spk_emb(stream):
            for s in stream: yield s
    
    ds = wds.WebDataset([input]).compose(
        wds.decode(),
        lambda x: split(x, copy_keys=['gain_shift.npy'], split_keys=['powers.npy']),
        utils.merge_in(utils.derived_dataset('spk_emb')),
        lambda x: merge_by_src_key(x, copy_keys=['gain_shift.npy'], merge_keys=['powers.npy', 'vad.npy', 'spk_emb.npy']),
        filter_bad_samples,
        chg_spk_emb,
        chunk_merger('raw', lambda x: True),
        chunk_merger('eq', random_cutter),
        chunk_merger('max')
    )

    with utils.AtomicTarWriter(output) as sink:
        for s in progress_bar(ds, total='noinfer'):
#             if len(s['vad.npy']) > 1:
#                 print(s)
            del s['vad.npy'], s['spk_emb.npy'], s['powers.npy']
            sink.write(s)

# %% ../nbs/1C. VAD merging.ipynb 22
def find_vad_kind(kind):
    def _finder(stream):
        for s in stream:
            for k in ['vad.npy', 'spk_emb.npy']:
                s[k] = s[f'{kind}.{k}']
            yield s
    return _finder

def chunked_audio_dataset(shards, kind='max', copy_keys=['gain_shift.npy'], split_keys=['spk_emb.npy'],
                          resampled=False, nodesplitter=wds.shardlists.single_node_only):
    return wds.WebDataset(shards, resampled=resampled, nodesplitter=nodesplitter).compose(
        wds.decode(utils.torch_audio_opus),
        utils.merge_in(utils.derived_dataset('mvad')),
        utils.find_audio,
        find_vad_kind(kind),
        lambda x: utils.split_to_chunks(x, copy_keys=copy_keys, split_keys=split_keys),
    )
