# Copyright 2022 the Regents of the University of California, Nerfstudio Team and contributors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Nerfacto augmented with depth supervision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple, Type

import torch
import numpy as np

from nerfstudio.cameras.rays import RayBundle
from nerfstudio.model_components import losses
from nerfstudio.model_components.losses import DepthLossType, depth_loss, depth_ranking_loss
from nerfstudio.models.nerfacto import NerfactoModel, NerfactoModelConfig
from nerfstudio.utils import colormaps


@dataclass
class DepthNerfactoModelConfig(NerfactoModelConfig):
    """Additional parameters for depth supervision."""

    _target: Type = field(default_factory=lambda: DepthNerfactoModel)
    depth_loss_mult: float = 0.02
    """Lambda of the depth loss."""
    uncertainty_weight: float = 1.0
    """Weight of the uncertainty in the loss if uncertainty weighted loss is used."""
    is_euclidean_depth: bool = False
    """Whether input depth maps are Euclidean distances (or z-distances)."""
    depth_sigma: float = 0.01
    """Uncertainty around depth values in meters (defaults to 1cm)."""
    should_decay_sigma: bool = False
    """Whether to exponentially decay sigma."""
    starting_depth_sigma: float = 0.2
    """Starting uncertainty around depth values in meters (defaults to 0.2m)."""
    sigma_decay_rate: float = 0.99985
    """Rate of exponential decay."""
    depth_loss_type: DepthLossType = DepthLossType.SIMPLE_LOSS
    """Depth loss type."""


class DepthNerfactoModel(NerfactoModel):
    """Depth loss augmented nerfacto model.

    Args:
        config: Nerfacto configuration to instantiate model
    """

    config: DepthNerfactoModelConfig

    def populate_modules(self):
        """Set the fields and modules."""
        super().populate_modules()

        if self.config.should_decay_sigma:
            self.depth_sigma = torch.tensor([self.config.starting_depth_sigma])
        else:
            self.depth_sigma = torch.tensor([self.config.depth_sigma])
            
    def get_outputs(self, ray_bundle: RayBundle):
        outputs = super().get_outputs(ray_bundle)
        if ray_bundle.metadata is not None and "directions_norm" in ray_bundle.metadata:
            outputs["directions_norm"] = ray_bundle.metadata["directions_norm"]
        return outputs

    def get_metrics_dict(self, outputs, batch):
        metrics_dict = super().get_metrics_dict(outputs, batch)
        if self.training:
            if (
                losses.FORCE_PSEUDODEPTH_LOSS
                and self.config.depth_loss_type not in losses.PSEUDODEPTH_COMPATIBLE_LOSSES
            ):
                raise ValueError(
                    f"Forcing pseudodepth loss, but depth loss type ({self.config.depth_loss_type}) must be one of {losses.PSEUDODEPTH_COMPATIBLE_LOSSES}"
                )
            if self.config.depth_loss_type in (DepthLossType.DS_NERF, DepthLossType.URF, DepthLossType.SIMPLE_LOSS, DepthLossType.DEPTH_UNCERTAINTY_WEIGHTED_LOSS, DepthLossType.DENSE_DEPTH_PRIORS_LOSS):
                metrics_dict["depth_loss"] = 0.0
                sigma = self._get_sigma().to(self.device)
                # get ground truth depth and uncertainty
                termination_depth = batch["depth_image"].to(self.device)
                
                termination_uncertainty = None
                if self.config.depth_loss_type in (DepthLossType.DEPTH_UNCERTAINTY_WEIGHTED_LOSS, DepthLossType.DENSE_DEPTH_PRIORS_LOSS):
                    termination_uncertainty = batch["depth_uncertainty"].to(self.device)
                # compute the depth loss for each weight
                for i in range(len(outputs["weights_list"])):
                    metrics_dict["depth_loss"] += depth_loss(
                        weights=outputs["weights_list"][i],
                        ray_samples=outputs["ray_samples_list"][i],
                        termination_depth=termination_depth,
                        predicted_depth=outputs["expected_depth"],
                        sigma=sigma,
                        directions_norm=outputs["directions_norm"],
                        is_euclidean=self.config.is_euclidean_depth,
                        depth_loss_type=self.config.depth_loss_type,
                        termination_uncertainty=termination_uncertainty,
                        predicted_uncertainty=outputs["depth_uncertainty"],
                        uncertainty_weight=self.config.uncertainty_weight,
                    ) / len(outputs["weights_list"])
            elif self.config.depth_loss_type in (DepthLossType.SPARSENERF_RANKING,):
                metrics_dict["depth_ranking"] = depth_ranking_loss(
                    outputs["expected_depth"], batch["depth_image"].to(self.device)
                )
            else:
                raise NotImplementedError(f"Unknown depth loss type {self.config.depth_loss_type}")

        return metrics_dict

    def get_loss_dict(self, outputs, batch, metrics_dict=None):
        loss_dict = super().get_loss_dict(outputs, batch, metrics_dict)
        if self.training:
            assert metrics_dict is not None and ("depth_loss" in metrics_dict or "depth_ranking" in metrics_dict)
            if "depth_ranking" in metrics_dict:
                loss_dict["depth_ranking"] = (
                    self.config.depth_loss_mult
                    * np.interp(self.step, [0, 2000], [0, 0.2])
                    * metrics_dict["depth_ranking"]
                )
            if "depth_loss" in metrics_dict:
                loss_dict["depth_loss"] = self.config.depth_loss_mult * metrics_dict["depth_loss"]
        return loss_dict

    def get_image_metrics_and_images(
        self, outputs: Dict[str, torch.Tensor], batch: Dict[str, torch.Tensor]
    ) -> Tuple[Dict[str, float], Dict[str, torch.Tensor]]:
        """Appends ground truth depth to the depth image."""
        scale = 0.25623789273
        metrics, images = super().get_image_metrics_and_images(outputs, batch)
        
        supervised_depth = batch["depth_image"].to(self.device) / scale
        
        outputs["depth"] = outputs["depth"] / scale
        
        ground_truth_depth_colormap = colormaps.apply_depth_colormap(supervised_depth)
        predicted_depth_colormap = colormaps.apply_depth_colormap(
            outputs["depth"],
            accumulation=outputs["accumulation"],
            near_plane=float(torch.min(supervised_depth).cpu()),
            far_plane=float(torch.max(supervised_depth).cpu()),
        )
        images["depth"] = torch.cat([ground_truth_depth_colormap, predicted_depth_colormap], dim=1)
        
        if supervised_depth.shape[1] == 899:
            supervised_depth = supervised_depth[:548, :898, :]
        
        supervised_depth_mask = supervised_depth > 0
        metrics["supervised_depth_mse"] = float(
            torch.nn.functional.mse_loss(outputs["depth"][supervised_depth_mask], supervised_depth[supervised_depth_mask]).cpu()
        ) / 7.27
        
        if "gt_object_depth_image" in batch and "gt_depth_image" in batch:
        
            gt_depth = batch["gt_depth_image"].to(self.device)
            
            gt_object_depth = batch["gt_object_depth_image"].to(self.device)
            
            print(gt_depth.shape, gt_object_depth.shape)
            
            depth_mask = gt_depth > 0
            metrics["gt_depth_mse"] = float(
                torch.nn.functional.mse_loss(outputs["depth"][depth_mask], gt_depth[depth_mask]).cpu()
            ) / 7.27
            
            object_depth_mask = gt_object_depth > 0
            metrics["gt_object_depth_mse"] = float(
                torch.nn.functional.mse_loss(outputs["depth"][object_depth_mask], gt_object_depth[object_depth_mask]).cpu()
            ) / 7.27
        return metrics, images

    def _get_sigma(self):
        if not self.config.should_decay_sigma:
            return self.depth_sigma

        self.depth_sigma = torch.maximum(
            self.config.sigma_decay_rate * self.depth_sigma, torch.tensor([self.config.depth_sigma])
        )
        return self.depth_sigma
