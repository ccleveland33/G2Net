# -*- coding: utf-8 -*-
"""
Created on Tue Aug 17 23:02:45 2021

@author: salva
"""

import tensorflow as tf
import numpy as np
import warnings
from scipy import signal
from typing import Tuple, Mapping

from utilities import GeneralUtilities


##############################################################################

class CQTLayer(tf.keras.layers.Layer):
    """
    Constant Q Transform keras layer.
    """

    def __init__(
            self, 
            sample_rate: int = 22050, 
            hop_length: int = 512, 
            n_bins = 84,
            bins_per_octave: int = 12,
            f_band: Tuple[float, float] = (0., None),
            norm: int = 1,
            filter_scale: int = 1,
            window: str = "hann",
            center: bool = True, 
            pad_mode: str = "reflect",
            norm_type: str = "librosa",
            image_out: bool = True,
            perc_range: float = 0.05,
            minmax_init: Tuple[float, float] = (0, -1e7),
            **kwargs
        ) -> None:
        """

        Parameters
        ----------
        sample_rate : int, optional
            DESCRIPTION. The default is 22050.
        hop_length : int, optional
            DESCRIPTION. The default is 512.
        n_bins : TYPE, optional
            DESCRIPTION. The default is 84.
        bins_per_octave : int, optional
            DESCRIPTION. The default is 12.
        f_band : Tuple[float, float], optional
            DESCRIPTION. The default is (0.0, None).
        norm : int, optional
            DESCRIPTION. The default is 1.
        filter_scale : int, optional
            DESCRIPTION. The default is 1.
        window : str, optional
            DESCRIPTION. The default is "hann".

        Returns
        -------
        None
        """
    
        super(CQTLayer, self).__init__(**kwargs)
        self.sample_rate = sample_rate
        self.n_bins = n_bins
        self.hop_length = hop_length
        self.perc_range = perc_range

        q = np.float(filter_scale) / (2. ** (1. / bins_per_octave) - 1.)
        cqt_kernels, kernel_width, lengths, _ = _Utilities.create_cqt_kernels(
            q, sample_rate, f_band[0], f_band[-1], n_bins, bins_per_octave, 
            norm, window)

        cqt_kernels_real = np.swapaxes(cqt_kernels.real[:, np.newaxis, :], 0, -1)
        cqt_kernels_imag = np.swapaxes(cqt_kernels.imag[:, np.newaxis, :], 0, -1)
        
        self.cqt_kernels_real = tf.Variable(initial_value = cqt_kernels_real, 
                                            trainable = self.trainable,
                                            name = self.name + "/real_kernels", 
                                            dtype = self.dtype)
        self.cqt_kernels_imag = tf.Variable(initial_value = cqt_kernels_imag, 
                                            trainable = self.trainable,
                                            name = self.name + "/imag_kernels",
                                            dtype = self.dtype)

        padding = tf.constant([[0, 0], [kernel_width // 2, kernel_width // 2],
                               [0, 0]])
    
        self.padding_fn = lambda x: x
        if center:
            if pad_mode == "constant":
                self.padding_fn = lambda x: tf.pad(x, padding, mode = "CONSTANT")
            elif pad_mode == "reflect":
                self.padding_fn = lambda x: tf.pad(x, padding, mode = "REFLECT")
            else:
                warnings.warn("Padding method not recognised, applying no padding", 
                              SyntaxWarning)
                
        self.norm_factor = 1.
        lengths = tf.constant(lengths, dtype = self.cqt_kernels_real.dtype)
        if norm_type == "librosa":
            self.norm_factor = tf.math.sqrt(lengths)
        elif norm_type == "convolutional":
            self.norm_factor = 1.
        elif norm_type == "wrap":
            self.norm_factor = 2.
        else:
            warnings.warn("Normalization method not recognised, \
                          applying convolutional normalization", 
                          SyntaxWarning)
                
        self.image_out = image_out
        self.max = tf.Variable(initial_value = minmax_init[-1], 
                               name = self.name + "/max", 
                               dtype = self.dtype)
        self.min = tf.Variable(initial_value = minmax_init[0], 
                               name = self.name + "/min", 
                               dtype = self.dtype)


    def build(
            self, 
            input_shape: Tuple[int, int, int]
        ) -> None:
        if self.trainable:
            self.trainable_weights.append(self.cqt_kernels_real)
            self.trainable_weights.append(self.cqt_kernels_imag)
        else:
            self.non_trainable_weights.append(self.cqt_kernels_real)
            self.non_trainable_weights.append(self.cqt_kernels_imag)
        self.non_trainable_weights.append(self.max)
        self.non_trainable_weights.append(self.min)
        super(CQTLayer, self).build(input_shape)


    def call(
            self, 
            data: tf.Tensor,
            training: bool = None
        ) -> tf.Tensor:
        """
        Forward pass of the layer.

        Parameters
        ----------
        data : tf.Tensor, shape = (None, n_samples, n_detectors)
            A batch of input mono waveforms, n_detectors should be last

        Returns
        -------
        tf.Tensor, shape = (None, n_time, n_freq, n_detectors)
            The corresponding batch of constant Q transforms.
        """

        CQT = []
        for i in range(data.get_shape()[-1]):
            x = data[..., i]
            x = GeneralUtilities.broadcast_dim(x)
            x = tf.cast(x, self.dtype)
            x = self.padding_fn(x)
            x_real = tf.nn.conv1d(x, self.cqt_kernels_real, 
                                  stride = self.hop_length, 
                                  padding = "VALID")
            x_imag = -tf.nn.conv1d(x, self.cqt_kernels_imag, 
                                   stride = self.hop_length, 
                                   padding = "VALID")
            x_real *= self.norm_factor
            x_imag *= self.norm_factor
            x = tf.pow(x_real, 2) + tf.pow(x_imag, 2)
            if self.trainable:
                x += 1e-8
            x = tf.math.sqrt(x)
            x = tf.transpose(x, [0, 2, 1])
            x = tf.expand_dims(x, axis = -1)
            CQT = x if (i == 0) else tf.concat([CQT, x], axis = -1)
            
        if self.image_out:
            if training:
                max_batch = tf.stop_gradient(tf.reduce_max(CQT))
                max_val = tf.stop_gradient(tf.math.maximum(self.max, max_batch))
                min_batch = tf.stop_gradient(tf.reduce_min(CQT))
                min_val = tf.stop_gradient(tf.math.minimum(self.min, min_batch))
        
                self.max.assign(max_val)
                self.min.assign(min_val)

            r_minmax = tf.stop_gradient(self.max - self.min)
            min_val = tf.stop_gradient(self.min)
            max_val = tf.stop_gradient(self.max + self.perc_range * r_minmax)
            CQT = (CQT - min_val)/(max_val - min_val)
            CQT *= 255.

        return CQT



    def get_config(
            self
        ) -> Mapping[str, float]:
        """
        Function to get the configuration parameters of the object.
        
        Returns
        -------
        Mapping[str, float]
            Dictionary containing the configuration parameters of the object.
        """
        config = {

        }
        
        config.update(super(CQTLayer, self).get_config())
        return config


##############################################################################


class _Utilities(object):
    """
    Class with auxiliary functions for CQT Layer.
    """

    @staticmethod
    def create_cqt_kernels(
            q: float,
            fs: float,
            f_min: float,
            f_max: float = None,
            n_bins: int = 84,
            bins_per_octave: int = 12,
            norm: float = 1,
            window: str = "hann",

            topbin_check: bool = True
        ) -> Tuple[np.ndarray, int, np.ndarray, float]:

        len_min = np.ceil(q * fs / f_min)
        fft_len = 2 ** np.int(np.ceil(np.log2(len_min)))

    
        if (f_max is not None) and (n_bins is None):
            n_bins = np.ceil(bins_per_octave * np.log2(f_max / f_min))
            freqs = f_min * 2. ** (np.r_[0:n_bins] / np.float(bins_per_octave))
        elif (f_max is None) and (n_bins is not None):
            freqs = f_min * 2. ** (np.r_[0:n_bins] / np.float(bins_per_octave))
        else:
            warnings.warn("If f_max is given, n_bins will be ignored", SyntaxWarning)
            n_bins = np.ceil(bins_per_octave * np.log2(f_max / f_min))
            freqs = f_min * 2. ** (np.r_[0:n_bins] / np.float(bins_per_octave))

        
        if np.max(freqs) > fs / 2. and topbin_check:
            raise ValueError(f"The top bin {np.max(freqs)} Hz has exceeded \
                             the Nyquist frequency, please reduce `n_bins`")

        kernel = np.zeros((np.int(n_bins), np.int(fft_len)), dtype = np.complex64)
    
        lengths = np.ceil(q * fs / freqs)
        for k in range(np.int(n_bins)):
            freq = freqs[k]
            l = np.ceil(q * fs / freq)

            if l % 2 == 1:
                start = np.int(np.ceil(fft_len / 2. - l / 2.)) - 1
            else:
                start = np.int(np.ceil(fft_len / 2. - l / 2.))
    
            sig = signal.get_window(window, np.int(l), fftbins = True)
            sig = sig * np.exp(np.r_[-l // 2:l // 2] * 1j * 2 * np.pi * freq / fs) / l
            
            if norm:
                kernel[k, start:start + np.int(l)] = sig / np.linalg.norm(sig, norm)
            else:
                kernel[k, start:start + np.int(l)] = sig

        return kernel, fft_len, lengths, freqs

    
##############################################################################

