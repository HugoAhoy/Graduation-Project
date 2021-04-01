import torch
import numpy as np
import torch.nn as nn
import random
from prune_util import get_preceding_from_succeding, get_filter_num_by_layer
from prune_util import unet_succeeding_strategy

'''
for get coresponding input and output
'''
class hookYandX:
    self.y = []
    self.x = []
    def __init__(self, layer,activation_kernel=None):
        self.activation_kernel = activation_kernel
        if activation_kernel is None:
            self.activation_kernel = list(range(layer.weight.shape[1]))
        inch = len(activation_kernel)
        outch, k, p, s = layer.in_channel, layer.out_channel, layer.kernel_size, layer.padding, layer.stride
        self.conv = nn.Conv2d(inch, outch,k, groups=1, padding=p, stride=s, bias=False)
        self.channel_wise = nn.Conv2d(inch, outch*inch,k, groups=inch, padding=p, stride=s, bias=False)
        self.conv.weight.data = layer.weight.data[:,self.activation_kernel,...]
        self.channel_wise.weight.data = layer.weight.data[:,self.activation_kernel,...].reshape(outch*inch, 1, k[0],k[1])
        self.hook = layer.register_forward_hook(self.hook_fn)
    
    def hook_fn(self, module, input, output):
        input = input[:,self.activation_kernel,...]
        self.conv = self.conv.cuda()
        self.y.append(self.conv(input).detach())
        self.channel_wise = self.channel_wise.cuda()
        self.x.append(self.channel_wise(input).detach())

    def remove(self):
        self.hook.remove()

'''
collecting training examples for layer pruning
'''
def collecting_training_examples(model, layer, train_loader,activation_kernel=None, m=1000):
    assert activation_kernel is not None
    in_and_out = hookYandX(layer)
    inch = len(activation_kernel)
    model.eval()
    for i, train_data in enumerate(train_loader):
        with torch.no_grad():
            train_data['L'] = train_data['L'].cuda()
            model(train_data['L'])
    
    y = tf.concat(in_and_out.y,0).contiguous().view(-1,1)
    samplesize = y.shape[0]

    m = min(m, samplesize)
    selected_index = random.sample(range(samplesize), m)
    x = tf.concat(in_and_out.x, 0).contiguous().view(samplesize, inch)

    in_and_out.remove()
    
    y = y[selected_index,:]
    x = x[selected_index,:]
    return (x, y), len(selected_index)


def get_subset(x, y, r, C):
    '''
    x:shape(sample_size, C)
    y:shape(sample_size, 1)
    r is the compression ratio as mentioned in paper;
    C is the channel num of the filter
    '''

    T = int(C*(1-r))
    res = []

    # greedy
    for iter in range(T):
        min_value = float('inf')
        tempRes = None
        for i in range(C):
            if i in res:
                continue
            else:
                tempT = res + [i]
                mask = torch.sum(torch.eye(C)[tempT,:], dim=1, keepdim=True) # mask shape(iter, C)
                diff = y-torch.sum(mask*x, dim=1,keepdim=True)
                total_error = torch.sum(diff**2)
                if total_error < min_value:
                    min_value = total_error
                    tempRes = tempT
        res = tempRes
    return res


def get_w_by_LSE(x, y):
    a = torch.matmul(torch.transpose(x,0,1),x)
    if torch.matrix_rank(a) == a.shape[0]:
        a_inv = torch.inverse(a)
    else:
        a_inv = torch.pinverse(a)
    w = torch.chain_matmul(a_inv, torch.transpose(x, 0,1), y)
    return w

def get_layers(model):
    layers = []
    for i in model.modules():
        if isinstance(i, nn.Conv2d):
            layers.append(i)
    return layers

def get_conv_nums(model):
    num = 0
    for i in model.modules():
        if isinstance(i, nn.Conv2d):
            num += 1
    return num


def thinet_prune_layer(model,layer_idx, train_loader, r, m=1000):
    succeeding_strategy = unet_succeeding_strategy(4)
    succeeding_layer_idx = succeeding_strategy[layer_idx]
    preceding_strategy = get_preceding_from_succeding(succeeding_layer_idx)

    all_layers = get_layers(model)
    layer = all_layers[layer_idx]
    C = layer.out_channel
    assert C == layer.weight.shape[0] # assure the layer is pruned for the first time.

    '''
    here, we should know the number of succeeding layers(#sc for short).
    if the #sc is >= 2, then the training examples are the sum of each layer's training examples.
    of course, the number of training examples must be same.
    if the #sc is == 1, no extra processing, just use the collecting_training_examples(···).

    When collecting the training examples, we should know 
    the pacesetter(or preceeding layer) number of the succeeding layer(#psc for short).
    if the #psc is >= 2, then the input of this layer is concat of several inputs, 
    we should use kernel index to identify the kernel that should be blind out.
    so collecting_training_examples(···) should add a parameter named activation_kernel.
    if the activation_kernel is None, this means all kernels are activated
    (in other word, no kernel is blind out).

    if the activation_kernel is not None, only the indexed kernel is activated, others are blind out.
    '''
    x_list, y_list = [], []
    min_sample_num = float('inf')
    for sl in succeeding_layer_idx:
        precedding_layers = preceding_strategy[sl]

        activation_kernel = list(range(C))

        # this snippet can handle concat more than 2 inputs, not only 2
        if len(precedding_layers) > 1:
            kernel_before = 0
            for pl in precedding_layers:
                if layer_idx > pl:
                    kernel_before += all_layers[pl].weight.shape[0]
                else:
                    break
            activation_kernel = list(range(kernel_before, kernel_before+C))
        tempx, tempy, sample_num = collecting_training_examples(model, all_layers[sl], train_loader,activation_kernel, m)
        min_sample_num = min(min_sample_num, sample_num)
        x_list.append(tempx)
        y_list.append(tempy)
    '''
    here concat and sum are both ok.
    scaling or not are optional,too.
    '''
    for i in range(len(x_list)):
        x_list[i] = x_list[i][:min_sample_num,...]
        y_list[i] = y_list[i][:min_sample_num,...]
    x = torch.concat(x_list,0)
    y = torch.concat(y_list,0)
    prune_subset = get_subset(x, y, r, C)
    assert len(set(prune_subset)) == len(prune_subset) # assure no duplicate element


    '''
    prune the layer
    '''
    saved_subset = list(set(range(C))-set(prune_subset))
    w = get_w_by_LSE(x[:,saved_subset], y)
    w = w.unsqueeze(0).unsqueeze(-1) #shape(1,len(saved_subset),1,1)

    layer.weight.data = layer.weight.data[saved_subset, ...]
    assert layer.weight.data.shape[1] == w.shape[0] # filter num should be the same as the element num of w
    layer.weight.grad = None

    if layer.bias is not None:
        layer.bias.data = layer.bias.data[saved_subset]
        layer.bias.grad = None
    
    '''
    prune the succeeding layer
    '''
    for sl in succeeding_layer_idx:
        precedding_layers = preceding_strategy[sl]
        kernel_before = 0
        # this snippet can handle concat more than 2 inputs, not only 2
        if len(precedding_layers) > 1:
            for pl in precedding_layers:
                if layer_idx > pl:
                    kernel_before += all_layers[pl].weight.shape[0]
                else:
                    break
        offset_prune_subset = [kernel_before+i for i in prune_subset]
        all_kernels = all_layers[sl].weight.shape[1]
        kernel_saved_subset = list(set(range(all_kernels))-set(offset_prune_subset))
        saved_weight = all_layers[sl].weight.data[:,kernel_saved_subset,...]

        # scaling it by w
        scaling_kernel = list(range(kernel_before, kernel_before+len(saved_subset)))
        saved_weight[:,scaling_kernel,...] = saved_weight[:,scaling_kernel,...]*w

        all_layers[sl].weight.data = saved_weight
        all_layers[sl].weight.grad = None
        if sl.bias is not None:
            sl.bias.data = sl.bias.data[kernel_saved_subset]
            sl.bias.grad = None

    return model

def save_model()
    pass