# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import lpips
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import models
from core.utils import crop_boundary


def masked_mse_loss(pred, gt, mask=None):
    if mask is None:
        return F.mse_loss(pred, gt)
    else:
        sum_loss = F.mse_loss(pred, gt, reduction='none')
        ndim = sum_loss.shape[1]
        return torch.sum(sum_loss * mask) / (ndim * torch.sum(mask) + 1e-8)


def masked_l1_loss(pred, gt, mask=None):
    if mask is None:
        return F.l1_loss(pred, gt)
    else:
        if mask.shape[-2:] != pred.shape[-2:]:
            mask = F.interpolate(mask, size=pred.shape[-2:])
        sum_loss = F.l1_loss(pred, gt, reduction='none')
        ndim = sum_loss.shape[1]
        return torch.sum(sum_loss * mask) / (ndim * torch.sum(mask) + 1e-8)


class Vgg16(nn.Module):
    def __init__(self):
        super(Vgg16, self).__init__()
        features = models.vgg16(pretrained=True).features
        self.to_relu_1_2 = nn.Sequential()
        self.to_relu_2_2 = nn.Sequential()
        self.to_relu_3_3 = nn.Sequential()
        self.to_relu_4_3 = nn.Sequential()

        for x in range(4):
            self.to_relu_1_2.add_module(str(x), features[x])
        for x in range(4, 9):
            self.to_relu_2_2.add_module(str(x), features[x])
        for x in range(9, 16):
            self.to_relu_3_3.add_module(str(x), features[x])
        for x in range(16, 23):
            self.to_relu_4_3.add_module(str(x), features[x])

        # don't need the gradients, just want the features
        for param in self.parameters():
            param.requires_grad = False

    def forward(self, x):
        h = self.to_relu_1_2(x)
        h_relu_1_2 = h
        h = self.to_relu_2_2(h)
        h_relu_2_2 = h
        h = self.to_relu_3_3(h)
        h_relu_3_3 = h
        h = self.to_relu_4_3(h)
        h_relu_4_3 = h
        out = [h_relu_1_2, h_relu_2_2, h_relu_3_3, h_relu_4_3]
        return out


class Vgg19(nn.Module):
    def __init__(self, requires_grad=False):
        super(Vgg19, self).__init__()
        vgg_pretrained_features = models.vgg19(pretrained=True).features
        self.slice1 = torch.nn.Sequential()
        self.slice2 = torch.nn.Sequential()
        self.slice3 = torch.nn.Sequential()
        self.slice4 = torch.nn.Sequential()
        self.slice5 = torch.nn.Sequential()
        for x in range(2):
            self.slice1.add_module(str(x), vgg_pretrained_features[x])
        for x in range(2, 7):
            self.slice2.add_module(str(x), vgg_pretrained_features[x])
        for x in range(7, 12):
            self.slice3.add_module(str(x), vgg_pretrained_features[x])
        for x in range(12, 21):
            self.slice4.add_module(str(x), vgg_pretrained_features[x])
        for x in range(21, 30):
            self.slice5.add_module(str(x), vgg_pretrained_features[x])
        if not requires_grad:
            for param in self.parameters():
                param.requires_grad = False

    def forward(self, x):
        h_relu1 = self.slice1(x)
        h_relu2 = self.slice2(h_relu1)
        h_relu3 = self.slice3(h_relu2)
        h_relu4 = self.slice4(h_relu3)
        h_relu5 = self.slice5(h_relu4)
        out = [h_relu1, h_relu2, h_relu3, h_relu4, h_relu5]
        return out


class VGGLoss(nn.Module):
    def __init__(self, model='vgg19', device='cuda'):
        super().__init__()
        if model == 'vgg16':
            self.vgg = Vgg16().to(device)
            self.weights = [1.0/16, 1.0/8, 1.0/4, 1.0]
        elif model == 'vgg19':
            self.vgg = Vgg19().to(device)
            self.weights = [1.0/32, 1.0/16, 1.0/8, 1.0/4, 1.0]
            # self.weights = [1/2.6, 1/4.8, 1/3.7, 1/5.6, 10/1.5]
            # self.weights = [1/2.6, 1/4.8, 1/3.7, 1/5.6, 2/1.5]
        # self.criterion = nn.L1Loss()
        self.loss_func = masked_l1_loss

    @staticmethod
    def preprocess(x, size=224):
        # B, C, H, W
        device = x.device
        mean = torch.tensor([0.485, 0.456, 0.406]).to(device)
        std = torch.tensor([0.229, 0.224, 0.225]).to(device)
        x = (x - mean.reshape(1, 3, 1, 1)) / std.reshape(1, 3, 1, 1)
        return x

    def forward(self, x, y, mask=None, size=224):
        x = self.preprocess(x, size=size)    # assume x, y are inside (0, 1)
        y = self.preprocess(y, size=size)

        if mask is not None:
            if min(mask.shape[-2:]) <= size:
                mode = 'bilinear'
                align_corners = True
            else:
                mode = 'area'
                align_corners = None
            mask = F.interpolate(mask, size=size, mode=mode, align_corners=align_corners)
        x_vgg, y_vgg = self.vgg(x), self.vgg(y)
        # loss = 0
        loss = self.loss_func(x, y, mask)
        for i in range(len(x_vgg)):
            loss += self.weights[i] * self.loss_func(x_vgg[i], y_vgg[i], mask)
        return loss


def normalize_minus_one_to_one(x):
    x_min = x.min()
    x_max = x.max()
    return 2. * (x - x_min) / (x_max - x_min) - 1.


def get_flow_smoothness_loss(flow, alpha):
    flow_gradient_x = flow[:, :, :, 1:, :] - flow[:, :, :, -1:, :]
    flow_gradient_y = flow[:, :, :, :, 1:] - flow[:, :, :, :, -1:]
    cost_x = (alpha[:, :, :, 1:, :] * torch.norm(flow_gradient_x, dim=2, keepdim=True)).sum()
    cost_y = (alpha[:, :, :, :, 1:] * torch.norm(flow_gradient_y, dim=2, keepdim=True)).sum()
    avg_cost = (cost_x + cost_y) / (2 * alpha.sum() + 1e-6)
    return avg_cost


class Criterion(nn.Module):
    def __init__(self, args):
        super(Criterion, self).__init__()
        device = "cuda:{}".format(args.local_rank)
        self.args = args
        self.crop_ratio = args.boundary_crop_ratio
        self.loss_mode = args.loss_mode
        if 'vgg' in self.loss_mode:
            self.loss_func = VGGLoss(model=self.loss_mode, device=device)
        elif self.loss_mode == 'lpips':
            self.loss_func = lpips.LPIPS(net='vgg').to(device)
        elif self.loss_mode == 'mse':
            self.loss_func = masked_mse_loss
        elif self.loss_mode == 'l1':
            self.loss_func = masked_l1_loss
        else:
            raise NotImplementedError

    def forward(self, pred, target, mask, is_multi_view, res_dict, scalar_to_log, step):
        if self.crop_ratio > 0:
            pred = crop_boundary(pred, self.crop_ratio)
            target = crop_boundary(target, self.crop_ratio)

        if self.loss_mode == 'lpips':
            pred_normed = 2 * pred - 1.
            target_normed = 2 * target - 1.
            loss = self.loss_func(pred_normed, target_normed)
            scalar_to_log['loss_perceptual'] = loss.item()

            if is_multi_view == 0:
                l1_loss = masked_l1_loss(pred, target, mask)
                loss = loss + l1_loss
                scalar_to_log['loss_l1'] = l1_loss.item()
        else:
            loss = self.loss_func(pred, target, mask)

        return loss, scalar_to_log

