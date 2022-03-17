from functools import reduce
from typing import Callable, Iterable, List, Optional, Tuple, Union

import torch
from icecream import ic  # noqa
from torch import Tensor, nn
from torch.nn.parameter import Parameter

from df.config import Csv, DfParams, config
from df.modules import GroupedGRU, Mask, erb_fb
from df.utils import angle_re_im, get_device
from libdf import DF


class ModelParams(DfParams):
    section = "multistagenet"

    def __init__(self):
        super().__init__()
        self.stages: List[int] = config(
            "STAGES", cast=Csv(int), default=(3, 3, 9, 3), section=self.section  # type: ignore
        )
        self.conv_lookahead: int = config(
            "CONV_LOOKAHEAD", cast=int, default=0, section=self.section
        )
        self.erb_widths: List[int] = config(
            "ERB_WIDTHS", cast=Csv(int), default=[16, 16, 16], section=self.section
        )
        self.erb_hidden_dim: int = config(
            "ERB_HIDDEN_DIM", cast=int, default=64, section=self.section
        )
        self.refinement_widths: List[int] = config(
            "REFINEMENT_WIDTHS", cast=Csv(int), default=[32, 32, 32, 32], section=self.section
        )
        self.refinement_hidden_dim: int = config(
            "REFINEMENT_HIDDEN_DIM", cast=int, default=96, section=self.section
        )
        self.refinement_act: str = (
            config("REFINEMENT_OUTPUT_ACT", default="identity", section=self.section)
            .lower()
            .replace("none", "identity")
        )
        self.refinement_op: str = config(
            "REFINEMENT_OP", default="mul", section=self.section
        ).lower()
        self.mask_pf: bool = config("MASK_PF", cast=bool, default=False, section=self.section)


class Conv2dNormAct(nn.Sequential):
    def __init__(
        self,
        in_ch: int,
        out_ch: int,
        kernel_size: Union[int, Iterable[int]],
        fstride: int = 1,
        dilation: int = 1,
        groups: int = 1,
        fpad: bool = True,
        bias: bool = True,
        norm_layer: Optional[Callable[..., torch.nn.Module]] = torch.nn.BatchNorm2d,
        activation_layer: Optional[Callable[..., torch.nn.Module]] = torch.nn.ReLU,
    ):
        """Causal Conv2d by delaying the signal for any lookahead.

        Expected input format: [B, C, T, F]
        """
        super().__init__()
        lookahead = 0  # This needs to be handled on the input feature side
        # Padding on time axis
        kernel_size = (
            (kernel_size, kernel_size) if isinstance(kernel_size, int) else tuple(kernel_size)
        )
        if fpad:
            fpad_ = kernel_size[1] // 2 + dilation - 1
        else:
            fpad_ = 0
        pad = (0, 0, lookahead, kernel_size[0] - 1 - lookahead)
        layers = []
        if any(x > 0 for x in pad):
            layers.append(nn.ConstantPad2d(pad, 0.0))
        layers.append(
            nn.Conv2d(
                in_ch,
                out_ch,
                kernel_size=kernel_size,
                padding=(0, fpad_),
                stride=(1, fstride),  # Stride over time is always 1
                dilation=(1, dilation),  # Same for dilation
                groups=groups,
                bias=bias,
            )
        )
        if norm_layer is not None:
            layers.append(norm_layer(out_ch))
        if activation_layer is not None:
            layers.append(activation_layer())
        super().__init__(*layers)


class ConvTranspose2dNormAct(nn.Sequential):
    def __init__(
        self,
        in_ch: int,
        out_ch: int,
        kernel_size: Union[int, Tuple[int, int]],
        fstride: int = 1,
        dilation: int = 1,
        groups: int = 1,
        fpad: bool = True,
        bias: bool = True,
        norm_layer: Optional[Callable[..., torch.nn.Module]] = torch.nn.BatchNorm2d,
        activation_layer: Optional[Callable[..., torch.nn.Module]] = torch.nn.ReLU,
    ):
        """Causal ConvTranspose2d.

        Expected input format: [B, C, T, F]
        """
        super().__init__()
        # Padding on time axis, with lookahead = 0
        kernel_size = (kernel_size, kernel_size) if isinstance(kernel_size, int) else kernel_size
        if fpad:
            fpad_ = kernel_size[1] // 2
        else:
            fpad_ = 0
        pad = (0, 0, 0, kernel_size[0] - 1)
        layers = []
        if any(x > 0 for x in pad):
            layers.append(nn.ConstantPad2d(pad, 0.0))
        layers.append(
            nn.ConvTranspose2d(
                in_ch,
                out_ch,
                kernel_size=kernel_size,
                padding=(kernel_size[0] - 1, fpad_ + dilation - 1),
                output_padding=(0, fpad_),
                stride=(1, fstride),  # Stride over time is always 1
                dilation=(1, dilation),
                groups=groups,
                bias=bias,
            )
        )
        if norm_layer is not None:
            layers.append(norm_layer(out_ch))
        if activation_layer is not None:
            layers.append(activation_layer())
        super().__init__(*layers)


class GruSE(nn.Module):
    """GRU with previous adaptive avg pooling like SqueezeExcitation"""

    def __init__(
        self,
        input_dim: int,
        hidden_dim: int,
        groups: int = 1,
        skip: Optional[Callable[..., torch.nn.Module]] = nn.Identity,
        scale_activation: Optional[Callable[..., torch.nn.Module]] = None,
    ):
        super().__init__()
        self.avg_dim = 3
        if groups == 1:
            self.gru = nn.GRU(input_dim, hidden_dim)
        else:
            self.gru = GroupedGRU(input_dim, hidden_dim, groups=groups)
        assert (
            skip or scale_activation is None
        ), "Can only either use a skip connection or SqueezeExcitation with `scale_activation`"
        self.fc = nn.Linear(hidden_dim, input_dim)
        self.skip = skip() if skip is not None else None
        self.scale = scale_activation() if scale_activation is not None else None

    def forward(self, input: Tensor, h=None) -> Tuple[Tensor, Tensor]:
        # x: [B, C, T, F]
        x = input.mean(dim=self.avg_dim)  # [B, C, T]
        x = x.transpose(1, 2)  # [B, T, C]
        x, h = self.gru(x, h)
        x = self.fc(x).transpose(1, 2).unsqueeze(-1)
        if self.skip is not None:
            x = self.skip(input) + x  # a regular skip connection
        elif self.scale is not None:
            x = input * self.scale(x)  # like in SqueezeExcitation
        return x, h


class LSNRNet(nn.Module):
    def __init__(
        self, in_ch: int, hidden_dim: int = 16, fstride=2, lsnr_min: int = -15, lsnr_max: int = 40
    ):
        super().__init__()
        self.conv = Conv2dNormAct(in_ch, in_ch, kernel_size=(1, 3), fstride=fstride, groups=in_ch)
        self.gru_snr = nn.GRU(in_ch, hidden_dim)
        self.fc_snr = nn.Sequential(nn.Linear(hidden_dim, 1), nn.Sigmoid())
        self.lsnr_scale = lsnr_max - lsnr_min
        self.lsnr_offset = lsnr_min

    def forward(self, x: Tensor, h=None) -> Tuple[Tensor, Tensor]:
        x = self.conv(x)
        x, h = self.gru_snr(x.mean(-1).transpose(1, 2), h)
        x = self.fc_snr(x) * self.lsnr_scale + self.lsnr_offset
        return x, h


class EncLayer(nn.Module):
    def __init__(
        self,
        in_ch,
        out_ch,
        kernel,
        fstride: int,
        gru_dim: int,
        gru_groups=1,
        gru_mode: str = "skip",
    ):
        super().__init__()
        self.conv = Conv2dNormAct(in_ch, out_ch, kernel_size=kernel, fstride=fstride)
        assert gru_mode in ("skip", "scale")
        if gru_mode == "skip":
            skip = nn.Identity
            scale = None
        else:
            skip = None
            scale = nn.Sigmoid
        self.gru = GruSE(out_ch, gru_dim, groups=gru_groups, skip=skip, scale_activation=scale)

    def forward(self, input: Tensor, h=None) -> Tuple[Tensor, Tensor]:
        # x: [B, C, T, F]
        x = self.conv(input)
        x, h = self.gru(x, h)
        return x, h


class FreqStage(nn.Module):
    def __init__(
        self,
        in_ch: int,
        out_ch: int,
        out_act: Optional[Callable[..., torch.nn.Module]],
        num_freqs: int,
        gru_dim: Union[int, List[int]],
        widths: List[int],
        fstrides: Optional[List[int]] = None,
        initial_kernel: Tuple[int, int] = (3, 3),
        kernel: Tuple[int, int] = (1, 3),
        # squeeze_exitation_factors: Optional[List[float]] = None,
        # groups: int = 1,
    ):
        super().__init__()
        self.fe = num_freqs  # Number of frequency bins in embedding
        self.in_ch = in_ch
        self.out_ch = out_ch
        self.depth = len(widths) - 1
        if fstrides is not None:
            assert len(fstrides) == self.depth
            overall_stride = reduce(lambda x, y: x * y, fstrides)
        else:
            fstrides = [2] * self.depth
            overall_stride = 2 ** (len(widths) - 1)
        # if squeeze_exitation_factors is not None:
        #     assert len(squeeze_exitation_factors) == self.depth
        assert num_freqs % overall_stride == 0, f"num_freqs ({num_freqs}) must correspond to depth"
        self.hd = gru_dim
        norm_layer = nn.BatchNorm2d

        self.enc0 = Conv2dNormAct(
            in_ch, widths[0], initial_kernel, fstride=1, norm_layer=norm_layer
        )
        self.enc = nn.ModuleList()

        if isinstance(gru_dim, int):
            gru_dim = [gru_dim] * self.depth
        fstrides = fstrides or [2] * self.depth
        for i in range(self.depth):
            in_ch = widths[i]
            out_ch = widths[i + 1]
            fstride = fstrides[i]
            self.enc.append(EncLayer(in_ch, out_ch, kernel, fstride, gru_dim=gru_dim[i]))

        self.dec = nn.ModuleList()
        for i in range(self.depth - 1, -1, -1):
            in_ch = widths[i + 1]
            out_ch = widths[i]
            fstride = fstrides[i]
            self.dec.append(
                ConvTranspose2dNormAct(in_ch, out_ch, kernel_size=kernel, fstride=fstride)
            )
        self.dec0 = Conv2dNormAct(
            widths[0],
            self.out_ch,
            kernel,
            fstride=1,
            norm_layer=norm_layer,
            activation_layer=out_act,
        )

    def encode(self, x: Tensor, h=None) -> Tuple[Tensor, List[Tensor], List[Tensor]]:
        intermediate = []
        if h is None:
            h = [None] * self.depth
        x = self.enc0(x)
        for i, enc_layer in enumerate(self.enc):
            intermediate.append(x)
            x, _ = enc_layer(x, h[i])
        return x, intermediate, h

    def decode(self, x: Tensor, intermediate: List[Tensor]) -> Tensor:
        for dec_layer, x_enc in zip(self.dec, reversed(intermediate)):
            x = dec_layer(x) + x_enc
        x = self.dec0(x)
        return x

    def forward(self, x: Tensor, h=None) -> Tuple[Tensor, Tensor, List[Tensor]]:
        # input shape: [B, C, T, F]
        # x_rnn, h = self.rnn(x, h)
        x_inner, intermediate, h = self.encode(x, h)
        x = self.decode(x_inner, intermediate)
        return x, x_inner, h


class ComplexCompression(nn.Module):
    def __init__(self, n_freqs: int, init_value: float = 0.5):
        super().__init__()
        self.c: Tensor
        self.register_parameter(
            "c", Parameter(torch.full((n_freqs,), init_value), requires_grad=True)
        )

    def forward(self, x: Tensor):
        # x has shape x [B, 2, T, F]
        x_abs = (x[:, 0].square() + x[:, 1].square()).clamp_min(1e-10).pow(self.c)
        x_ang = angle_re_im.apply(x[:, 0], x[:, 1])
        x = torch.stack((x_abs * torch.cos(x_ang), x_abs * torch.sin(x_ang)), dim=1)
        # x_c = x_abs * torch.exp(1j * x_ang)
        # x_ = torch.view_as_complex(x.permute(0,2,3,1).contiguous())
        # torch.allclose(x_, x_c)
        return x


class MagCompression(nn.Module):
    def __init__(self, n_freqs: int, init_value: float = 0.5):
        super().__init__()
        self.c: Tensor
        self.register_parameter(
            "c", Parameter(torch.full((n_freqs,), init_value), requires_grad=True)
        )

    def forward(self, x: Tensor):
        # x has shape x [B, T, F, 2]
        x = x.pow(self.c)
        return x


class ComplexAdd(nn.Module):
    def forward(self, a, b):
        return a + b


class ComplexMul(nn.Module):
    def forward(self, a, b):
        # [B, 2, *]
        re = a[:, :1] * b[:, :1] - a[:, 1:] * b[:, 1:]
        im = a[:, :1] * b[:, 1:] + a[:, :1] * b[:, 1:]
        return torch.cat((re, im), dim=1)


class MSNet(nn.Module):
    def __init__(self, erb_fb: Tensor, erb_inv_fb: Tensor):
        super().__init__()
        p = ModelParams()
        self.stages = p.stages
        self.freq_bins = p.fft_size // 2 + 1
        self.erb_bins = p.nb_erb
        self.df_bins = p.nb_df
        self.erb_fb: Tensor
        self.erb_comp = MagCompression(self.erb_bins)
        self.cplx_comp = ComplexCompression(self.df_bins)
        self.register_buffer("erb_fb", erb_fb, persistent=False)
        self.erb_stage = FreqStage(
            in_ch=1,
            out_ch=1,
            widths=p.erb_widths,
            out_act=nn.Sigmoid,
            num_freqs=p.nb_erb,
            gru_dim=p.erb_hidden_dim,
        )
        self.mask = Mask(erb_inv_fb, post_filter=p.mask_pf)
        refinement_act = {"tanh": nn.Tanh, "identity": nn.Identity}[p.refinement_act.lower()]
        self.refinement_stage = FreqStage(
            in_ch=2,
            out_ch=2,
            out_act=refinement_act,
            widths=p.refinement_widths,
            num_freqs=p.nb_df,
            gru_dim=p.refinement_hidden_dim,
        )
        self.refinement_op = ComplexMul() if p.refinement_op == "mul" else ComplexAdd()
        self.lsnr_net = LSNRNet(p.erb_widths[-1], lsnr_min=p.lsnr_min, lsnr_max=p.lsnr_max)

    def forward(
        self, spec: Tensor, atten_lim: Optional[Tensor] = None, **kwargs  # type: ignore
    ) -> Tuple[Tensor, Tensor, Tensor, None]:
        # Spec shape: [B, 1, T, F, 2]
        feat_erb = torch.view_as_complex(spec).abs().matmul(self.erb_fb)
        feat_erb = self.erb_comp(feat_erb)
        m, x_rnn, _ = self.erb_stage(feat_erb)
        spec = self.mask(spec, m, atten_lim)  # [B, 1, T, F, 2]
        lsnr, _ = self.lsnr_net(x_rnn)
        # re/im into channel axis
        spec_f = (
            spec.squeeze(1)[:, :, : self.df_bins].permute(0, 3, 1, 2).clone()
        )  # [B, 2, T, F_df]
        r, _, _ = self.refinement_stage(self.cplx_comp(spec_f))
        spec_f = self.refinement_op(spec_f, r)
        spec[..., : self.df_bins, :] = spec_f.unsqueeze(-1).transpose(1, -1)
        return spec, m, lsnr, None


def init_model(df_state: Optional[DF] = None, run_df: bool = True, train_mask: bool = True):
    assert run_df and train_mask
    p = ModelParams()
    if df_state is None:
        df_state = DF(sr=p.sr, fft_size=p.fft_size, hop_size=p.hop_size, nb_bands=p.nb_erb)
    erb_width = df_state.erb_widths()
    erb = erb_fb(erb_width, p.sr)
    erb_inverse = erb_fb(erb_width, p.sr, inverse=True)
    model = MSNet(erb, erb_inverse)
    return model.to(device=get_device())
