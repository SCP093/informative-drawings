import torch.nn as nn
import torch.nn.functional as F
import torch
import functools
from torchvision import models
from torch.autograd import Variable
import numpy as np
import math
import re


# Creates SPADE normalization layer based on the given configuration
# SPADE consists of two steps. First, it normalizes the activations using
# your favorite normalization method, such as Batch Norm or Instance Norm.
# Second, it applies scale and bias to the normalized output, conditioned on
# the segmentation map.
# The format of |config_text| is spade(norm)(ks), where
# (norm) specifies the type of parameter-free normalization.
#       (e.g. syncbatch, batch, instance)
# (ks) specifies the size of kernel in the SPADE module (e.g. 3x3)
# Example |config_text| will be spadesyncbatch3x3, or spadeinstance5x5.
# Also, the other arguments are
# |norm_nc|: the #channels of the normalized activations, hence the output dim of SPADE
# |label_nc|: the #channels of the input semantic map, hence the input dim of SPADE
class SPADE(nn.Module):
    def __init__(self, config_text, norm_nc, label_nc):
        super().__init__()

        assert config_text.startswith('spade')
        parsed = re.search('spade(\D+)(\d)x\d', config_text)
        param_free_norm_type = str(parsed.group(1))
        ks = int(parsed.group(2))

        if param_free_norm_type == 'instance':
            self.param_free_norm = nn.InstanceNorm2d(norm_nc, affine=False)
        elif param_free_norm_type == 'batch':
            self.param_free_norm = nn.BatchNorm2d(norm_nc, affine=False)
        else:
            raise ValueError('%s is not a recognized param-free norm type in SPADE'
                             % param_free_norm_type)

        # The dimension of the intermediate embedding space. Yes, hardcoded.
        nhidden = 128

        pw = ks // 2
        self.mlp_shared = nn.Sequential(
            nn.Conv2d(label_nc, nhidden, kernel_size=ks, padding=pw),
            nn.ReLU()
        )
        self.mlp_gamma = nn.Conv2d(nhidden, norm_nc, kernel_size=ks, padding=pw)
        self.mlp_beta = nn.Conv2d(nhidden, norm_nc, kernel_size=ks, padding=pw)

    def forward(self, x, segmap):

        # Part 1. generate parameter-free normalized activations
        normalized = self.param_free_norm(x)

        # Part 2. produce scaling and bias conditioned on semantic map
        segmap = F.interpolate(segmap, size=x.size()[2:], mode='nearest')
        actv = self.mlp_shared(segmap)
        gamma = self.mlp_gamma(actv)
        beta = self.mlp_beta(actv)

        # apply scale and bias
        out = normalized * (1 + gamma) + beta

        return out


norm_layer = nn.InstanceNorm2d


class SPADEResidualBlock(nn.Module):
    def __init__(self, in_features):
        super(SPADEResidualBlock, self).__init__()

        conv_block0 = [nn.ReflectionPad2d(1),
                       nn.Conv2d(in_features, in_features, 3)
                       ]
        self.conv_block0 = nn.Sequential(*conv_block0)
        self.norm_layer0 = SPADE("spadeinstance3x3", in_features, 3)
        conv_block1 = [nn.ReLU(inplace=True),
                       nn.ReflectionPad2d(1),
                       nn.Conv2d(in_features, in_features, 3)
                       ]
        self.conv_block1 = nn.Sequential(*conv_block1)
        self.norm_layer1 = SPADE("spadeinstance3x3", in_features, 3)

    def forward(self, x, depth):
        out = self.conv_block0(x)
        out = self.norm_layer0(out, depth)
        out = self.conv_block1(out)
        out = self.norm_layer1(out, depth)
        return x + out


class ResidualBlock(nn.Module):
    def __init__(self, in_features):
        super(ResidualBlock, self).__init__()

        conv_block = [nn.ReflectionPad2d(1),
                      nn.Conv2d(in_features, in_features, 3),
                      norm_layer(in_features),
                      nn.ReLU(inplace=True),
                      nn.ReflectionPad2d(1),
                      nn.Conv2d(in_features, in_features, 3),
                      norm_layer(in_features)
                      ]

        self.conv_block = nn.Sequential(*conv_block)

    def forward(self, x):
        return x + self.conv_block(x)


class SPADEGenerator(nn.Module):
    def __init__(self, input_nc, output_nc, n_residual_blocks=9, sigmoid=True):
        super(SPADEGenerator, self).__init__()

        # Initial convolution block
        model0 = [nn.ReflectionPad2d(3),
                  nn.Conv2d(input_nc, 64, 7),
                  norm_layer(64),
                  nn.ReLU(inplace=True)]
        self.model0 = nn.Sequential(*model0)

        # Downsampling
        model1 = []
        in_features = 64
        out_features = in_features*2
        for _ in range(2):
            model1 += [nn.Conv2d(in_features, out_features, 3, stride=2, padding=1),
                       norm_layer(out_features),
                       nn.ReLU(inplace=True) ]
            in_features = out_features
            out_features = in_features*2
        self.model1 = nn.Sequential(*model1)

        model2 = []
        # Residual blocks
        for _ in range(n_residual_blocks):
            model2 += [SPADEResidualBlock(in_features)]
        self.model2 = nn.Sequential(*model2)

        # Upsampling
        model3 = []
        out_features = in_features//2
        for _ in range(2):
            model3 += [nn.ConvTranspose2d(in_features, out_features, 3, stride=2, padding=1, output_padding=1),
                       # norm_layer(out_features),
                       SPADE("spadeinstance3x3", out_features, 3),
                       nn.ReLU(inplace=True)]
            in_features = out_features
            out_features = in_features//2
        self.model3 = nn.Sequential(*model3)

        # Output layer
        model4 = [nn.ReflectionPad2d(3),
                  nn.Conv2d(64, output_nc, 7)]
        if sigmoid:
            model4 += [nn.Sigmoid()]

        self.model4 = nn.Sequential(*model4)

    def forward(self, x, depth, sign=0, cond=None):
        random_tensor = torch.rand(1, dtype=depth.dtype, device=depth.device) * 2
        random_tensor.floor_()
        if sign == 0:  # depth is random zero tensor
            depth = depth * random_tensor
        elif sign == 1:  # depth is depth
            depth = depth
        elif sign == 2:  # depth is zero tensor
            depth = depth * 0
        out = self.model0(x)
        out = self.model1(out)
        for m in self.model2:
            out = m(out, depth)
        for m in self.model3:
            if isinstance(m, SPADE):
                out = m(out, depth)
            else:
                out = m(out)
        out = self.model4(out)

        return out


class Generator(nn.Module):
    def __init__(self, input_nc, output_nc, n_residual_blocks=9, sigmoid=True):
        super(Generator, self).__init__()

        # Initial convolution block
        model0 = [   nn.ReflectionPad2d(3),
                    nn.Conv2d(input_nc, 64, 7),
                    norm_layer(64),
                    nn.ReLU(inplace=True) ]
        self.model0 = nn.Sequential(*model0)

        # Downsampling
        model1 = []
        in_features = 64
        out_features = in_features*2
        for _ in range(2):
            model1 += [  nn.Conv2d(in_features, out_features, 3, stride=2, padding=1),
                        norm_layer(out_features),
                        nn.ReLU(inplace=True) ]
            in_features = out_features
            out_features = in_features*2
        self.model1 = nn.Sequential(*model1)

        model2 = []
        # Residual blocks
        for _ in range(n_residual_blocks):
            model2 += [ResidualBlock(in_features)]
        self.model2 = nn.Sequential(*model2)

        # Upsampling
        model3 = []
        out_features = in_features//2
        for _ in range(2):
            model3 += [  nn.ConvTranspose2d(in_features, out_features, 3, stride=2, padding=1, output_padding=1),
                        norm_layer(out_features),
                        nn.ReLU(inplace=True) ]
            in_features = out_features
            out_features = in_features//2
        self.model3 = nn.Sequential(*model3)

        # Output layer
        model4 = [  nn.ReflectionPad2d(3),
                        nn.Conv2d(64, output_nc, 7)]
        if sigmoid:
            model4 += [nn.Sigmoid()]

        self.model4 = nn.Sequential(*model4)

    def forward(self, x, cond=None):
        out = self.model0(x)
        out = self.model1(out)
        out = self.model2(out)
        out = self.model3(out)
        out = self.model4(out)

        return out

# Define a resnet block
class ResnetBlock(nn.Module):
    def __init__(self, dim, padding_type, norm_layer, activation=nn.ReLU(True), use_dropout=False):
        super(ResnetBlock, self).__init__()
        self.conv_block = self.build_conv_block(dim, padding_type, norm_layer, activation, use_dropout)

    def build_conv_block(self, dim, padding_type, norm_layer, activation, use_dropout):
        conv_block = []
        p = 0
        if padding_type == 'reflect':
            conv_block += [nn.ReflectionPad2d(1)]
        elif padding_type == 'replicate':
            conv_block += [nn.ReplicationPad2d(1)]
        elif padding_type == 'zero':
            p = 1
        else:
            raise NotImplementedError('padding [%s] is not implemented' % padding_type)

        conv_block += [nn.Conv2d(dim, dim, kernel_size=3, padding=p),
                       norm_layer(dim),
                       activation]
        if use_dropout:
            conv_block += [nn.Dropout(0.5)]

        p = 0
        if padding_type == 'reflect':
            conv_block += [nn.ReflectionPad2d(1)]
        elif padding_type == 'replicate':
            conv_block += [nn.ReplicationPad2d(1)]
        elif padding_type == 'zero':
            p = 1
        else:
            raise NotImplementedError('padding [%s] is not implemented' % padding_type)
        conv_block += [nn.Conv2d(dim, dim, kernel_size=3, padding=p),
                       norm_layer(dim)]

        return nn.Sequential(*conv_block)

    def forward(self, x):
        out = x + self.conv_block(x)
        return out

class GlobalGenerator2(nn.Module):
    def __init__(self, input_nc, output_nc, ngf=64, n_downsampling=3, n_blocks=9, norm_layer=nn.BatchNorm2d, 
                 padding_type='reflect', use_sig=False, n_UPsampling=0):
        assert(n_blocks >= 0)
        super(GlobalGenerator2, self).__init__()        
        activation = nn.ReLU(True)        

        mult = 8
        model = [nn.ReflectionPad2d(4), nn.Conv2d(input_nc, ngf*mult, kernel_size=7, padding=0), norm_layer(ngf*mult), activation]

        ### downsample
        for i in range(n_downsampling):
            model += [nn.ConvTranspose2d(ngf * mult, ngf * mult // 2, kernel_size=4, stride=2, padding=1),
                      norm_layer(ngf * mult // 2), activation]
            mult = mult // 2

        if n_UPsampling <= 0:
            n_UPsampling = n_downsampling

        ### resnet blocks
        for i in range(n_blocks):
            model += [ResnetBlock(ngf * mult, padding_type=padding_type, activation=activation, norm_layer=norm_layer)]

        ### upsample         
        for i in range(n_UPsampling):
            next_mult = mult // 2
            if next_mult == 0:
                next_mult = 1
                mult = 1

            model += [nn.ConvTranspose2d(ngf * mult, int(ngf * next_mult), kernel_size=3, stride=2, padding=1, output_padding=1),
                       norm_layer(int(ngf * next_mult)), activation]
            mult = next_mult

        if use_sig:
            model += [nn.ReflectionPad2d(3), nn.Conv2d(ngf, output_nc, kernel_size=7, padding=0), nn.Sigmoid()]
        else:      
            model += [nn.ReflectionPad2d(3), nn.Conv2d(ngf, output_nc, kernel_size=7, padding=0), nn.Tanh()]        
        self.model = nn.Sequential(*model)
            
    def forward(self, input, cond=None):
        return self.model(input)


class InceptionV3(nn.Module): #avg pool
    def __init__(self, num_classes, isTrain, use_aux=True, pretrain=False, freeze=True, every_feat=False):
        super(InceptionV3, self).__init__()
        """ Inception v3 expects (299,299) sized images for training and has auxiliary output
        """

        self.every_feat = every_feat

        self.model_ft = models.inception_v3(pretrained=pretrain)
        stop = 0
        if freeze and pretrain:
            for child in self.model_ft.children():
                if stop < 17:
                    for param in child.parameters():
                        param.requires_grad = False
                stop += 1

        num_ftrs = self.model_ft.AuxLogits.fc.in_features #768
        self.model_ft.AuxLogits.fc = nn.Linear(num_ftrs, num_classes)

        # Handle the primary net
        num_ftrs = self.model_ft.fc.in_features #2048
        self.model_ft.fc = nn.Linear(num_ftrs,num_classes)

        self.model_ft.input_size = 299

        self.isTrain = isTrain
        self.use_aux = use_aux

        if self.isTrain:
            self.model_ft.train()
        else:
            self.model_ft.eval()


    def forward(self, x, cond=None, catch_gates=False):
        # N x 3 x 299 x 299
        x = self.model_ft.Conv2d_1a_3x3(x)

        # N x 32 x 149 x 149
        x = self.model_ft.Conv2d_2a_3x3(x)
        # N x 32 x 147 x 147
        x = self.model_ft.Conv2d_2b_3x3(x)
        # N x 64 x 147 x 147
        x = F.max_pool2d(x, kernel_size=3, stride=2)
        # N x 64 x 73 x 73
        x = self.model_ft.Conv2d_3b_1x1(x)
        # N x 80 x 73 x 73
        x = self.model_ft.Conv2d_4a_3x3(x)

        # N x 192 x 71 x 71
        x = F.max_pool2d(x, kernel_size=3, stride=2)
        # N x 192 x 35 x 35
        x = self.model_ft.Mixed_5b(x)
        feat1 = x
        # N x 256 x 35 x 35
        x = self.model_ft.Mixed_5c(x)
        feat11 = x
        # N x 288 x 35 x 35
        x = self.model_ft.Mixed_5d(x)
        feat12 = x
        # N x 288 x 35 x 35
        x = self.model_ft.Mixed_6a(x)
        feat2 = x
        # N x 768 x 17 x 17
        x = self.model_ft.Mixed_6b(x)
        feat21 = x
        # N x 768 x 17 x 17
        x = self.model_ft.Mixed_6c(x)
        feat22 = x
        # N x 768 x 17 x 17
        x = self.model_ft.Mixed_6d(x)
        feat23 = x
        # N x 768 x 17 x 17
        x = self.model_ft.Mixed_6e(x)

        feat3 = x

        # N x 768 x 17 x 17
        aux_defined = self.isTrain and self.use_aux
        if aux_defined:
            aux = self.model_ft.AuxLogits(x)
        else:
            aux = None
        # N x 768 x 17 x 17
        x = self.model_ft.Mixed_7a(x)
        # N x 1280 x 8 x 8
        x = self.model_ft.Mixed_7b(x)
        # N x 2048 x 8 x 8
        x = self.model_ft.Mixed_7c(x)
        # N x 2048 x 8 x 8
        # Adaptive average pooling
        x = F.adaptive_avg_pool2d(x, (1, 1))
        # N x 2048 x 1 x 1
        feats = F.dropout(x, training=self.isTrain)
        # N x 2048 x 1 x 1
        x = torch.flatten(feats, 1)
        # N x 2048
        x = self.model_ft.fc(x)
        # N x 1000 (num_classes)

        if self.every_feat:
            # return feat21, feats, x
            return x, feat21

        return x, aux