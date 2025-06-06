import matplotlib.pyplot as plt
import torch
from diffusers.utils import make_image_grid
from diffusers.utils.torch_utils import randn_tensor
import os
import numpy as np
from einops import repeat
#from tqdm.auto import tqdm
from typing import List, Optional, Tuple, Union
import copy

from .general_utils import rand_tensor

#'''
# differences are negligible, but this one is faster
@torch.no_grad()
def create_scatter_mask(tensor, 
                        channels: List[int] = None,
                        ratio: Union[float, torch.Tensor] = 0.1, 
                        x_idx = None, 
                        y_idx = None, 
                        generator = None, 
                        device = None):
    '''
    return a mask that has the same shape as the input tensor, if multiple channels are specified, the same mask will be applied to all channels
    tensor: torch.Tensor
    channels: list of ints, denote the idx of known channels, default None. If None, all channels are masked
    ratio: float or array-like, default 0.1. The ratio of known elements
    x_idx, y_idx: int, default None. If not None, the mask will be applied to the specified indices. OrientationL (0,0) is the top left corner
                  They can be either 2D or 1D tensors

    return: torch.Tensor (B, C, H, W)
    '''
    #TODO: handle generator
    if device is None:
        device = tensor.device
    B, C, H, W = tensor.shape
    if channels is None:
        channels = torch.arange(C, device=device)  # Ensure the same device as the input tensor
    else:
        channels = torch.tensor(channels, device=device)  # Ensure the same device as the input tensor

    # Create a random mask for all elements
    if x_idx is not None and y_idx is not None:
        mask = torch.zeros(B, 1, H, W, device=device)
        mask[:, :, y_idx, x_idx] = 1
        if len(channels) > 1:
            mask = repeat(mask, 'B 1 H W -> B C H W', C=len(channels))
    else:
        # For now, only support same mask for all channels
        mask = torch.zeros(B, 1, H, W, device=device)

        if isinstance(ratio, float) or ratio.numel() == 1:
            num_elements_to_select = int(H * W * ratio)
            ratios = [num_elements_to_select] * B
        else:
            ratios = [int(H * W * r) for r in ratio]

        for b in range(B):
            indices = torch.randperm(H * W, device=device)[:ratios[b]]
            mask[b, 0].view(-1)[indices] = 1

        if len(channels) > 1:
            mask = repeat(mask, 'B 1 H W -> B C H W', C=len(channels))

    # Initialize the final mask with zeros
    final_mask = torch.zeros_like(tensor)
    mask = mask.type_as(final_mask)
    final_mask[:, channels, :, :] = mask

    return final_mask

def create_patch_mask(tensor, channels=None, ratio=0.1):
    B, C, H, W = tensor.shape
    if channels is None:
        channels = range(C) # Assume apply to all channels
    patch_size = int(min(H, W) * ratio)
    start = (H - patch_size) // 2
    end = start + patch_size
    mask = torch.zeros_like(tensor)
    mask[:, channels] = 1
    mask[:, channels, start:end, start:end] = 0
    return mask

@torch.no_grad()
def edm_sampler_cond(
    net, noise_scheduler, batch_size=1, class_labels=None, randn_like=torch.randn_like,
    num_inference_steps=18, S_churn=0, S_min=0, S_max=float('inf'), S_noise=0,
    deterministic=True, mask=None, known_latents=None, known_channels=None,
    return_trajectory=False, add_noise_to_obs=False,
    generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
    device = 'cpu'
):
    '''
    mask: torch.Tensor, shape (H, W) or (B, C, H, W), 1 for known values, 0 for unknown
    known_latents: torch.Tensor, shape (H, W) or (B, C, H, W), known values
    '''
    if known_latents is not None:
        assert batch_size == known_latents.shape[0], "Batch size must match the known_latents shape"
        # Sample gaussian noise to begin loop

    if isinstance(device, str):
        device = torch.device(device)

    if isinstance(net.config.sample_size, int):
        latents_shape = (
            batch_size,
            net.config.out_channels,
            net.config.sample_size,
            net.config.sample_size,
        )
    else:
        latents_shape = (batch_size, net.config.out_channels, *net.config.sample_size)

    latents = randn_tensor(latents_shape, generator=generator, device=device, dtype=net.dtype)
    if add_noise_to_obs:
        noise = latents.clone()
    conditioning_tensors = torch.cat((known_latents, mask[:, [known_channels[0]]]), dim=1)
    noise_scheduler.set_timesteps(num_inference_steps, device=device)

    t_steps = noise_scheduler.sigmas.to(device)

    x_next = latents.to(torch.float64) * t_steps[0]
    if mask is not None:
        if len(mask.shape) == 2:
            mask = mask[None, None, ...].expand_as(x_next)
    else:
        mask = torch.zeros_like(x_next)
    if mask is not None:
        x_next = x_next * (1 - mask) + known_latents * mask

    if return_trajectory:
        whole_trajectory = torch.zeros((num_inference_steps, *x_next.shape), dtype=torch.float64)
    # Main sampling loop.
    for i, (t_cur, t_next) in enumerate(zip(t_steps[:-1], t_steps[1:])):  # 0, ..., N-1
        x_cur = x_next
        if not deterministic:
            # Increase noise temporarily.
            gamma = min(S_churn / num_inference_steps, np.sqrt(2) - 1) if S_min <= t_cur <= S_max else 0
            t_hat = torch.as_tensor(t_cur + gamma * t_cur)
            x_hat = x_cur + (t_hat ** 2 - t_cur ** 2).sqrt() * S_noise * randn_like(x_cur) * (1 - mask)
        else:
            t_hat = t_cur
            x_hat = x_cur

        if known_latents is not None and add_noise_to_obs:
            tmp_known_latents = known_latents.clone()
            tmp_known_latents = noise_scheduler.add_noise(tmp_known_latents, noise, t_hat.view(-1))
            x_hat = x_hat * (1 - mask) + tmp_known_latents * mask

        tmp_x_hat = x_hat.clone()
        c_noise = noise_scheduler.precondition_noise(t_hat)
        # Euler step.
        tmp_x_hat = noise_scheduler.precondition_inputs(tmp_x_hat, t_hat)

        denoised = net(tmp_x_hat.to(torch.float32), c_noise.reshape(-1).to(torch.float32), conditioning_tensors).sample.to(torch.float64)
        denoised = noise_scheduler.precondition_outputs(x_hat, denoised, t_hat)

        d_cur = (x_hat - denoised) / t_hat # denoise has the same shape as x_hat (b, out_channels, h, w)
        x_next = x_hat + (t_next - t_hat) * d_cur * (1 - mask)

        # Apply 2nd order correction.
        if i < num_inference_steps - 1:

            if known_latents is not None and add_noise_to_obs:
                tmp_known_latents = known_latents.clone()
                tmp_known_latents = noise_scheduler.add_noise(tmp_known_latents, noise, t_next.view(-1))
                x_next = x_next * (1 - mask) + tmp_known_latents * mask

            tmp_x_next = x_next.clone()
            c_noise = noise_scheduler.precondition_noise(t_next)
            
            tmp_x_next = noise_scheduler.precondition_inputs(tmp_x_next, t_next)

            denoised = net(tmp_x_next.to(torch.float32),c_noise.reshape(-1).to(torch.float32), conditioning_tensors).sample.to(torch.float64)
            denoised = noise_scheduler.precondition_outputs(x_next, denoised, t_next)

            d_prime = (x_next - denoised) / t_next
            x_next = x_hat + (t_next - t_hat) * (0.5 * d_cur + 0.5 * d_prime) * (1 - mask)

        if return_trajectory:
            whole_trajectory[i] = x_next

    if return_trajectory:
        return x_next, whole_trajectory
    else:
        return x_next

@torch.no_grad()
def edm_sampler_uncond(
    net, noise_scheduler, batch_size=1, class_labels=None, randn_like=torch.randn_like,
    num_inference_steps=18, S_churn=0, S_min=0, S_max=float('inf'), S_noise=0,
    deterministic=True, mask=None, known_channels=None, known_latents=None,
    return_trajectory=False,
    generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
    device = 'cpu'
):
    '''
    mask: torch.Tensor, shape (H, W) or (B, C, H, W), 1 for known values, 0 for unknown
    known_latents: torch.Tensor, shape (H, W) or (B, C, H, W), known values
    '''
    if known_latents is not None:
        assert batch_size == known_latents.shape[0], "Batch size must match the known_latents shape"
        # Sample gaussian noise to begin loop

    if isinstance(device, str):
        device = torch.device(device)

    if isinstance(net.config.sample_size, int):
        latents_shape = (
            batch_size,
            net.config.out_channels,
            net.config.sample_size,
            net.config.sample_size,
        )
    else:
        latents_shape = (batch_size, net.config.out_channels, *net.config.sample_size)

    latents = randn_tensor(latents_shape, generator=generator, device=device, dtype=net.dtype)
    noise = latents.clone()
    noise_scheduler.set_timesteps(num_inference_steps, device=device)

    t_steps = noise_scheduler.sigmas.to(device)

    x_next = latents.to(torch.float64) * t_steps[0] # edm start with max sigma
    if mask is not None:
        if len(mask.shape) == 2:
            mask = mask[None, None, ...].expand_as(x_next)
    else:
        mask = torch.zeros_like(x_next)

    if return_trajectory:
        whole_trajectory = torch.zeros((num_inference_steps, *x_next.shape), dtype=torch.float64)
    # Main sampling loop.
    for i, (t_cur, t_next) in enumerate(zip(t_steps[:-1], t_steps[1:])):  # 0, ..., N-1
        x_cur = x_next
        if not deterministic:
            # Increase noise temporarily.
            gamma = min(S_churn / num_inference_steps, np.sqrt(2) - 1) if S_min <= t_cur <= S_max else 0
            t_hat = torch.as_tensor(t_cur + gamma * t_cur)
            x_hat = x_cur + (t_hat ** 2 - t_cur ** 2).sqrt() * S_noise * randn_like(x_cur)
        else:
            t_hat = t_cur
            x_hat = x_cur

        if known_latents is not None:
            tmp_known_latents = known_latents.clone()
            tmp_known_latents = noise_scheduler.add_noise(tmp_known_latents, noise, t_hat.view(-1))
            x_hat = x_hat * (1 - mask) + tmp_known_latents * mask

        tmp_x_hat = x_hat.clone()
        c_noise = noise_scheduler.precondition_noise(t_hat)
        # Euler step.
        tmp_x_hat = noise_scheduler.precondition_inputs(tmp_x_hat, t_hat)

        denoised = net(tmp_x_hat.to(torch.float32), c_noise.reshape(-1).to(torch.float32), class_labels).sample.to(torch.float64)
        denoised = noise_scheduler.precondition_outputs(x_hat, denoised, t_hat)

        d_cur = (x_hat - denoised) / t_hat # denoise has the same shape as x_hat (b, out_channels, h, w)
        x_next = x_hat + (t_next - t_hat) * d_cur

        # Apply 2nd order correction.
        if i < num_inference_steps - 1:

            #"""
            if known_latents is not None:
                tmp_known_latents = known_latents.clone()
                tmp_known_latents = noise_scheduler.add_noise(tmp_known_latents, noise, t_next.view(-1))
                x_next = x_next * (1 - mask) + tmp_known_latents * mask
            #"""

            tmp_x_next = x_next.clone()
            c_noise = noise_scheduler.precondition_noise(t_next)
            """
            if mask is not None:
                tmp_x_next = torch.cat((tmp_x_next, concat_mask), dim=1)
            """
            
            tmp_x_next = noise_scheduler.precondition_inputs(tmp_x_next, t_next)

            denoised = net(tmp_x_next.to(torch.float32),c_noise.reshape(-1).to(torch.float32), class_labels).sample.to(torch.float64)
            denoised = noise_scheduler.precondition_outputs(x_next, denoised, t_next)

            d_prime = (x_next - denoised) / t_next
            x_next = x_hat + (t_next - t_hat) * (0.5 * d_cur + 0.5 * d_prime)

        if return_trajectory:
            whole_trajectory[i] = x_next

    if return_trajectory:
        return x_next, whole_trajectory
    else:
        return x_next

@torch.no_grad()
def ensemble_sample(pipeline, sample_size, mask, sampler_kwargs=None, class_labels=None, known_latents=None, 
                    batch_size=64, sampler_type: Optional[str] = 'edm', # 'edm' or 'pipeline'
                    device='cpu', conditioning_type='xattn', # 'xattn' or 'cfg'
                 ):
    batch_size_list = [batch_size]*int(sample_size/batch_size) + [sample_size % batch_size]
    #print(latents.shape, class_labels.shape, mask.shape, known_latents.shape)
    count = 0
    samples = torch.empty(sample_size, pipeline.unet.config.out_channels, pipeline.unet.config.sample_size,
                          pipeline.unet.config.sample_size, device=device, dtype=pipeline.unet.dtype)
    if sampler_kwargs is None:
        sampler_kwargs = {}
    if sampler_type == 'edm':
        model = pipeline.unet
        noise_scheduler = copy.deepcopy(pipeline.scheduler)
    for num_sample in batch_size_list:
        #tmp_class_labels = repeat(class_labels, 'C -> B C', B=num_sample)
        generator = [torch.Generator(device).manual_seed(int(seed) % (1 << 32)) for seed in range(count, count+num_sample)]
        tmp_mask = repeat(mask, '1 C H W -> B C H W', B=num_sample)
        tmp_known_latents = repeat(known_latents, '1 C H W -> B C H W', B=num_sample)
        if sampler_type == 'edm':
            if conditioning_type == 'xattn' or conditioning_type == 'cfg':
                tmp_samples = edm_sampler_cond(model, noise_scheduler, batch_size=num_sample, generator=generator, device=device,
                                        class_labels=class_labels, mask=tmp_mask, known_latents=tmp_known_latents, **sampler_kwargs)
            elif conditioning_type == 'uncond':
                tmp_samples = edm_sampler_uncond(model, noise_scheduler, batch_size=num_sample, generator=generator, device=device,
                                        class_labels=class_labels, mask=tmp_mask, known_latents=tmp_known_latents, **sampler_kwargs)
        elif sampler_type == 'pipeline':
            tmp_samples = pipeline(batch_size=num_sample, generator=generator, 
                                    mask=tmp_mask, known_latents=tmp_known_latents, return_dict=False, **sampler_kwargs)[0]
        samples[count:count+num_sample] = tmp_samples
        count += num_sample
    return samples

def colored_noise(shape, noise_type='pink', device='cpu', normalize=False):
    """
    Generate colored noise (pink, red, blue, purple) in the spatial domain.
    
    Args:
        shape (tuple): Shape of the noise tensor (b, c, h, w).
        noise_type (str): Type of noise ('white', 'pink', 'red', 'blue', 'purple').
        device (str): Device for the tensor.
        normalize (bool): Whether to normalize the output to [-1, 1] range.
        
    Returns:
        torch.Tensor: Colored noise tensor (b, c, h, w).
    """
    if len(shape) != 4:
        raise ValueError("Input shape must be of the form (b, c, h, w)")
    
    valid_noise_types = ['white', 'pink', 'red', 'blue', 'purple']
    if noise_type not in valid_noise_types:
        raise ValueError(f"Noise type must be one of {valid_noise_types}")
    
    b, c, h, w = shape
    
    # Initialize the output noise tensor
    output_noise = torch.zeros(shape, device=device)
    
    # Loop over the batch and channel dimensions
    for batch in range(b):
        for channel in range(c):
            # Generate white noise for the current (h, w) slice
            white_noise = torch.randn(h, w, device=device)
            
            # Apply Fourier transform to convert to frequency domain
            noise_fft = torch.fft.rfftn(white_noise, dim=(-2, -1))
            
            # Create frequency grid for both dimensions
            freqs_x = torch.fft.fftfreq(h, d=1.0).to(device)
            freqs_y = torch.fft.rfftfreq(w, d=1.0).to(device)
            
            # Generate 2D frequency grid
            freq_grid = torch.sqrt(freqs_x[:, None]**2 + freqs_y[None, :]**2)
            eps = torch.finfo(freq_grid.dtype).eps
            
            # Modify the amplitude spectrum based on the type of colored noise
            spectral_factors = {
                'white': lambda f: torch.ones_like(f),
                'pink': lambda f: 1.0 / torch.sqrt(f + eps),
                'red': lambda f: 1.0 / (f + eps),
                'blue': lambda f: torch.sqrt(f + eps),
                'purple': lambda f: f + eps
            }
            
            factor = spectral_factors[noise_type](freq_grid)
            
            # Handle DC component (zero frequency) specially
            if noise_type in ['pink', 'red']:
                factor[0, 0] = 1.0
            
            # Multiply the amplitude spectrum by the factor
            noise_fft *= factor
            
            # Inverse Fourier transform back to the spatial domain
            colored_noise = torch.fft.irfftn(noise_fft, s=(h, w), dim=(-2, -1))
            
            # Normalize if requested
            if normalize:
                colored_noise = 2.0 * (colored_noise - colored_noise.min()) / (colored_noise.max() - colored_noise.min()) - 1.0
            
            # Assign the generated noise to the corresponding slice in the output tensor
            output_noise[batch, channel] = colored_noise
    
    return output_noise