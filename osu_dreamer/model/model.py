from typing import List, Tuple

import copy

import numpy as np
import librosa

import torch
import torch.nn.functional as F

try:
    import matplotlib.pyplot as plt
    USE_MATPLOTLIB = True
except:
    USE_MATPLOTLIB = False

import pytorch_lightning as pl

from .beta_schedule import CosineBetaSchedule, StridedBetaSchedule
from .modules import UNet

VALID_PAD = 1024

class Model(pl.LightningModule):
    def __init__(
        self,
        h_dim: int,
        h_dim_groups: int,
        dim_mults: List[int],
        convnext_mult: int,
        wave_stack_depth: int,
        wave_num_stacks: int,
        
        timesteps: int,
        sample_steps: int,
    
        loss_type: str,
        timing_dropout: float,
        learning_rate: float = 0.,
        learning_rate_schedule_factor: float = 0.,
    ):
        super().__init__()
        self.save_hyperparameters()
        
        # model
        self.net = UNet(
            h_dim, h_dim_groups, dim_mults, 
            convnext_mult,
            wave_stack_depth,
            wave_num_stacks,
        )
        
        self.schedule = CosineBetaSchedule(timesteps, self.net)
        self.sampling_schedule = StridedBetaSchedule(self.schedule, sample_steps, self.net)
        
        # training params
        try:
            self.loss_fn = dict(
                l1 = F.l1_loss,
                l2 = F.mse_loss,
                huber = F.smooth_l1_loss,
            )[loss_type]
        except KeyError:
            raise NotImplementedError(loss_type)

        self.learning_rate = learning_rate
        self.learning_rate_schedule_factor = learning_rate_schedule_factor
        self.timing_dropout = timing_dropout
        self.depth = len(dim_mults)
        
    def inference_pad(self, x):
        x = F.pad(x, (VALID_PAD, VALID_PAD), mode='replicate')
        pad = (1 + x.size(-1) // 2 ** self.depth) * 2 ** self.depth - x.size(-1)
        x = F.pad(x, (0, pad), mode='replicate')
        return x, (..., slice(VALID_PAD,-(VALID_PAD+pad)))
        
    def forward(self, a: "N,A,L", t: "N,T,L", **kwargs):
        a, sl = self.inference_pad(a)
        t, _  = self.inference_pad(t)
        return self.sampling_schedule.sample(a, t, **kwargs)[sl]
    
    
#
#
# =============================================================================
# MODEL TRAINING
# =============================================================================
#
#

    def compute_loss(self, a, t, p, x, pad=False, timing_dropout=0.):
        ts = torch.randint(0, self.schedule.timesteps, (x.size(0),), device=x.device).long()
        
        if pad:
            a, _ = self.inference_pad(a)
            t, _ = self.inference_pad(t)
            p, _ = self.inference_pad(p)
            x, _ = self.inference_pad(x)
            
        if timing_dropout > 0:
            drop_idxs = torch.randperm(t.size(0))[:int(t.size(0) * timing_dropout)]
            t[drop_idxs] = p[drop_idxs]
        
        true_eps: "N,X,L" = torch.randn_like(x)

        x_t: "N,X,L" = self.schedule.q_sample(x, ts, true_eps)
        
        pred_eps = self.net(x_t, a, t, ts)
        
        return self.loss_fn(true_eps, pred_eps).mean()

    def configure_optimizers(self):
        opt = torch.optim.AdamW(self.net.parameters(), lr=self.learning_rate)
        
        return dict(
            optimizer=opt,
            lr_scheduler=dict(
                scheduler=torch.optim.lr_scheduler.ReduceLROnPlateau(
                    opt, factor=self.learning_rate_schedule_factor),
                monitor="val/loss",
            ),
        )
    
    def training_step(self, batch: Tuple["N,A,L", "N,T,L", "N,T,L", "N,X,L"], batch_idx):
        torch.cuda.empty_cache()
        a,t,p,x = copy.deepcopy(batch)
        
        loss = self.compute_loss(a,t,p,x,timing_dropout=self.timing_dropout)
        
        self.log(
            "train/loss", loss.detach(),
            logger=True, on_step=True, on_epoch=False,
        )
        
        return loss

    def validation_step(self, batch: Tuple["1,A,L","1,T,L","1,T,L","1,X,L"], batch_idx, *args, **kwargs):
        torch.cuda.empty_cache()
        a,t,p,x = copy.deepcopy(batch)
        
        loss = self.compute_loss(a,t,p,x, pad=True, timing_dropout=self.timing_dropout)
        dropout_loss = self.compute_loss(a,t,p,x, pad=True, timing_dropout=1.)
        
        self.log(
            "val/loss", loss.detach(),
            logger=True, on_step=False, on_epoch=True,
        )
        
        self.log(
            "val/dropout_loss", dropout_loss.detach(),
            logger=True, on_step=False, on_epoch=True,
        )
        
        return a,t,p,x
        
    def validation_epoch_end(self, val_outs: "List[(1,A,L),(1,T,L),(1,T,L),(1,X,L)]"):
        if not USE_MATPLOTLIB or len(val_outs) == 0:
            return
        
        torch.cuda.empty_cache()
        a,t,p,x = copy.deepcopy(val_outs[0])
        
        samples = self(a.repeat(2,1,1), torch.cat([ t,p ], dim=0) ).cpu().numpy()
        
        a: "A,L" = a.squeeze(0).cpu().numpy()
        x: "X,L" = x.squeeze(0).cpu().numpy()
        
        height_ratios = [1.5] + [1] * (1+len(samples))
        w, h = a.shape[-1]/150, sum(height_ratios)/2
        margin, margin_left = .1, .5
        
        fig, (ax1, *axs) = plt.subplots(
            len(height_ratios), 1,
            figsize=(w, h),
            sharex=True,
            gridspec_kw=dict(
                height_ratios=height_ratios,
                hspace=.1,
                left=margin_left/w,
                right=1-margin/w,
                top=1-margin/h,
                bottom=margin/h,
            )
        )
        
        ax1.imshow(librosa.power_to_db(a), origin="lower", aspect='auto')
        
        for sample, ax in zip((x, *samples), axs):
            mu = np.mean(sample)
            sig = np.std(sample)

            ax.set_ylim((mu-3*sig, mu+3*sig))
            
            for v in sample:
                ax.plot(v)

        self.logger.experiment.add_figure("samples", fig, global_step=self.global_step)
        plt.close(fig)
