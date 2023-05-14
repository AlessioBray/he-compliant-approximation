"""Module approximator for Multihead layers."""

import math
import warnings
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import torch
from torch import Tensor, nn
from torch.nn.functional import _in_projection  # type: ignore
from torch.nn.functional import _in_projection_packed  # type: ignore
from torch.nn.functional import _mha_shape_check  # type: ignore
from torch.nn.functional import dropout, linear
from torch.overrides import handle_torch_function, has_torch_function

from ..core import ModuleApproximator


class CustomizableMultiHeadApproximator(ModuleApproximator):
    """Handles the approximation of the multihead attention module.

    Attributes:
        supported_layer_types: contains the classes of the modules or functions that the approximator can approximate.
        approximation_type: name to identify the approximator referring to the type of approximation module.
        is_approximation_trainable: establishes if the approximation contain some trainable parameters.
    """

    supported_layer_types = {nn.MultiheadAttention}
    approximation_type = "customizable_multihead"
    is_approximation_trainable = True

    def __init__(
        self, parameters: Dict[str, Any] = {}, **kwargs: Dict[str, Any]
    ) -> None:
        """Initializes the CustomizableMultiHeadApproximator.

        Args:
            parameters: parameters of the CustomizableMultiHead modules. Defaults to {}.
        """
        super().__init__(parameters, **kwargs)
        self.approximations: List[CustomizableMultiHead] = []

    def approximate_module(
        self, model: nn.Module, id: str, pretrained: bool, **kwargs: Dict[str, Any]
    ) -> nn.Module:
        """Approximates the module identified by the id.

        Args:
            model: model that contains the module to be approximated.
            id: identifier of the module to be approximated.
            pretrained: specifies which kind of module approximation should be returned: trainable or pretrained version.

        Returns:
            approximated module.
        """

        # retrieving the multihead module that is going to be approximated
        kwargs = {"multihead": getattr(model, id)}
        if pretrained:
            return self.get_pretrained_approximation(module=getattr(model, id))
        else:
            return self.get_trainable_approximation(**kwargs)

    def get_trainable_approximation(self, **kwargs: Dict[str, Any]) -> nn.Module:
        """Approximates the module for the training phase.

        Returns:
            approximated module ready for the training phase.
        """
        # since the approximation allows customization of the attention mechanism
        # the approximation is built using the same arguments of the original multihead attention module
        original_multihead: nn.MultiheadAttention = kwargs["multihead"]  # type: ignore

        self.parameters["embed_dim"] = self.parameters.get(
            "embed_dim", original_multihead.embed_dim
        )
        self.parameters["num_heads"] = self.parameters.get(
            "num_heads", original_multihead.num_heads
        )
        self.parameters["batch_first"] = self.parameters.get(
            "batch_first", original_multihead.batch_first
        )
        self.parameters["dropout"] = self.parameters.get(
            "dropout", original_multihead.dropout
        )
        self.parameters["bias"] = self.parameters.get(
            "bias", True if original_multihead.in_proj_bias is not None else False
        )
        self.parameters["add_bias_kv"] = self.parameters.get(
            "add_bias_kv", True if original_multihead.bias_k is not None else False
        )
        self.parameters["add_zero_attn"] = self.parameters.get(
            "add_zero_attn", original_multihead.add_zero_attn
        )
        self.parameters["kdim"] = self.parameters.get("kdim", original_multihead.kdim)
        self.parameters["vdim"] = self.parameters.get("vdim", original_multihead.vdim)

        new_approximation = CustomizableMultiHead(**self.parameters)

        # loading the weights (parameters) of the multihead module that is going to be approximated
        for name, params in original_multihead.named_parameters():
            if len(name.split(".")) == 2:  # loading out_proj.weight and out_proj.bias
                param_path = name.split(".")
                module = getattr(new_approximation, param_path[0])
                setattr(module, param_path[1], params)
            elif len(name.split(".")) == 1:  # loading in_proj_weight and in_proj_bias
                setattr(new_approximation, name, params)
        # adding the module to the approximation list
        self.approximations.append(new_approximation)
        return new_approximation

    def get_pretrained_approximation(
        self, module: nn.Module, **kwargs: Dict[str, Any]
    ) -> nn.Module:
        """Converts the trainable approximation of the module into its pretrained form.

        Args:
            module: module approximation to be converted.

        Raises:
            ValueError: this method must be called for CustomizableMultiHead modules.

        Returns:
            approximated module in its pretrained form.
        """
        if not isinstance(module, CustomizableMultiHead):
            raise ValueError(f"{module.__class__} is not a {CustomizableMultiHead}")
        return module


def _scaled_dot_product(query: Tensor, key: Tensor) -> Tensor:
    """Scaled attention query-key dot product.
    Part of `torch.nn.functional._scaled_dot_product_attention`.

    Args:
        query: attention query values.
        key: attention key values.

    Returns:
        dot product between query and key matrices
    """
    B, Nt, E = query.shape
    query = query / math.sqrt(E)
    # (B, Nt, E) x (B, E, Ns) -> (B, Nt, Ns)
    return torch.bmm(query, key.transpose(-2, -1))


def _attn_masking(attn: Tensor, attn_mask: Tensor, attn_mask_value: float) -> Tensor:
    """Attention masking thorugh mask summation
    Part of `torch.nn.functional._scaled_dot_product_attention`.

    Args:
        attn: attention values.
        attn_mask: attention mask.
        attn_mask_value: masking value (i.e. what normally is -inf).

    Returns:
        masked attention values
    """
    if attn_mask is not None:
        attn += attn_mask
    return attn


def _scaled_dot_product_attention(
    q: Tensor,
    k: Tensor,
    v: Tensor,
    attn_mask: Optional[Tensor] = None,
    dropout_p: float = 0.0,
    kernel_function: Union[nn.Module, Callable] = nn.Softmax(dim=-1),
    attn_mask_value: float = float("-inf"),
    attn_masking_function: Union[nn.Module, Callable] = _attn_masking,
    query_key_product: Union[nn.Module, Callable] = _scaled_dot_product,
) -> Tuple[Tensor, Tensor]:
    """Reworked method from `torch.nn.functional._scaled_dot_product_attention`."""
    attn = query_key_product(q, k)

    attn = attn_masking_function(attn, attn_mask, attn_mask_value)

    attn = kernel_function(attn)  # (B, Nt, Ns)

    if dropout_p > 0.0:
        attn = dropout(attn, p=dropout_p)
    # (B, Nt, Ns) x (B, Ns, E) -> (B, Nt, E)
    output = torch.bmm(attn, v)
    return output, attn


class CustomizableMultiHead(nn.MultiheadAttention):
    """Multihead attention with customizable attention modules.

    Attributes:
        is_approximation_of: class of the approximated module/function.
    """

    is_approximation_of = nn.MultiheadAttention

    def __init__(
        self,
        embed_dim,
        num_heads,
        dropout=0.0,
        bias=True,
        add_bias_kv=False,
        add_zero_attn=False,
        kdim=None,
        vdim=None,
        batch_first=False,
        device=None,
        dtype=None,
        kernel_function: Union[nn.Module, Callable] = nn.Softmax(dim=-1),
        attn_mask_value: float = float("-inf"),
        attention_function: Union[nn.Module, Callable] = _scaled_dot_product_attention,
        attn_masking_function: Union[nn.Module, Callable] = _attn_masking,
        query_key_product: Union[nn.Module, Callable] = _scaled_dot_product,
    ) -> None:
        """Reworked method from `torch.nn.MultiheadAttention`.

        Added Args:
            kernel_function: function applied to the masked attention. Defaults to Softmax(dim=-1).
            attn_mask_value: masking value. Defaults to float("-inf").
            attention_function: function that implements the attention mechanism. Defaults to _scaled_dot_product_attention.
            attn_masking_function: function that implements the attention masking mechanism. Defaults to _attn_masking.
            query_key_product: function that implements the attention query-key product mechanism. Defaults to _scaled_dot_product.

        """
        super().__init__(
            embed_dim=embed_dim,
            num_heads=num_heads,
            dropout=dropout,
            bias=bias,
            add_bias_kv=add_bias_kv,
            add_zero_attn=add_zero_attn,
            kdim=kdim,
            vdim=vdim,
            batch_first=batch_first,
            device=device,
            dtype=dtype,
        )
        # added attributes w.r.t. `torch.nn.MultiheadAttention`
        self.kernel_function = kernel_function
        self.attn_mask_value = attn_mask_value
        self.attention_function = attention_function
        self.attn_masking_function = attn_masking_function
        self.query_key_product = query_key_product

    def forward(
        self,
        query: Tensor,
        key: Tensor,
        value: Tensor,
        key_padding_mask: Optional[Tensor] = None,
        need_weights: bool = True,
        attn_mask: Optional[Tensor] = None,
        average_attn_weights: bool = True,
    ) -> Tuple[Tensor, Optional[Tensor]]:
        """Reworked method from `torch.nn.MultiheadAttention`."""

        is_batched = query.dim() == 3
        if self.batch_first and is_batched:
            query, key, value = [x.transpose(1, 0) for x in (query, key, value)]

        if not self._qkv_same_embed_dim:
            attn_output, attn_output_weights = _multi_head_attention_forward(
                query,
                key,
                value,
                self.embed_dim,
                self.num_heads,
                self.in_proj_weight,
                self.in_proj_bias,
                self.bias_k,
                self.bias_v,
                self.add_zero_attn,
                self.dropout,
                self.out_proj.weight,
                self.out_proj.bias,
                training=self.training,
                key_padding_mask=key_padding_mask,
                need_weights=need_weights,
                attn_mask=attn_mask,
                use_separate_proj_weight=True,
                q_proj_weight=self.q_proj_weight,
                k_proj_weight=self.k_proj_weight,
                v_proj_weight=self.v_proj_weight,
                average_attn_weights=average_attn_weights,
                kernel_function=self.kernel_function,
                attn_mask_value=self.attn_mask_value,
                attention_function=self.attention_function,
                attn_masking_function=self.attn_masking_function,
                query_key_product=self.query_key_product,
            )
        else:
            attn_output, attn_output_weights = _multi_head_attention_forward(
                query,
                key,
                value,
                self.embed_dim,
                self.num_heads,
                self.in_proj_weight,
                self.in_proj_bias,
                self.bias_k,
                self.bias_v,
                self.add_zero_attn,
                self.dropout,
                self.out_proj.weight,
                self.out_proj.bias,
                training=self.training,
                key_padding_mask=key_padding_mask,
                need_weights=need_weights,
                attn_mask=attn_mask,
                average_attn_weights=average_attn_weights,
                kernel_function=self.kernel_function,
                attn_mask_value=self.attn_mask_value,
                attention_function=self.attention_function,
                attn_masking_function=self.attn_masking_function,
                query_key_product=self.query_key_product,
            )
        if self.batch_first and is_batched:
            return attn_output.transpose(1, 0), attn_output_weights
        else:
            return attn_output, attn_output_weights


def _multi_head_attention_forward(
    query: Tensor,
    key: Tensor,
    value: Tensor,
    embed_dim_to_check: int,
    num_heads: int,
    in_proj_weight: Tensor,
    in_proj_bias: Optional[Tensor],
    bias_k: Optional[Tensor],
    bias_v: Optional[Tensor],
    add_zero_attn: bool,
    dropout_p: float,
    out_proj_weight: Tensor,
    out_proj_bias: Optional[Tensor],
    training: bool = True,
    key_padding_mask: Optional[Tensor] = None,
    need_weights: bool = True,
    attn_mask: Optional[Tensor] = None,
    use_separate_proj_weight: bool = False,
    q_proj_weight: Optional[Tensor] = None,
    k_proj_weight: Optional[Tensor] = None,
    v_proj_weight: Optional[Tensor] = None,
    static_k: Optional[Tensor] = None,
    static_v: Optional[Tensor] = None,
    average_attn_weights: bool = True,
    kernel_function: Union[nn.Module, Callable] = nn.Softmax(dim=-1),
    attn_mask_value: float = float("-inf"),
    attention_function: Union[nn.Module, Callable] = _scaled_dot_product_attention,
    attn_masking_function: Union[nn.Module, Callable] = _attn_masking,
    query_key_product: Union[nn.Module, Callable] = _scaled_dot_product,
) -> Tuple[Tensor, Optional[Tensor]]:
    tens_ops = (
        query,
        key,
        value,
        in_proj_weight,
        in_proj_bias,
        bias_k,
        bias_v,
        out_proj_weight,
        out_proj_bias,
    )
    """Reworked method from `torch.nn.MultiheadAttention`."""
    if has_torch_function(tens_ops):
        return handle_torch_function(
            _multi_head_attention_forward,
            tens_ops,
            query,
            key,
            value,
            embed_dim_to_check,
            num_heads,
            in_proj_weight,
            in_proj_bias,
            bias_k,
            bias_v,
            add_zero_attn,
            dropout_p,
            out_proj_weight,
            out_proj_bias,
            training=training,
            key_padding_mask=key_padding_mask,
            need_weights=need_weights,
            attn_mask=attn_mask,
            use_separate_proj_weight=use_separate_proj_weight,
            q_proj_weight=q_proj_weight,
            k_proj_weight=k_proj_weight,
            v_proj_weight=v_proj_weight,
            static_k=static_k,
            static_v=static_v,
            kernel_function=kernel_function,
            attn_mask_value=attn_mask_value,
            attention_function=attention_function,
            attn_masking_function=attn_masking_function,
            query_key_product=query_key_product,
        )

    is_batched = _mha_shape_check(
        query, key, value, key_padding_mask, attn_mask, num_heads
    )

    # For unbatched input, we unsqueeze at the expected batch-dim to pretend that the input
    # is batched, run the computation and before returning squeeze the
    # batch dimension so that the output doesn't carry this temporary batch dimension.
    if not is_batched:
        # unsqueeze if the input is unbatched
        query = query.unsqueeze(1)
        key = key.unsqueeze(1)
        value = value.unsqueeze(1)
        if key_padding_mask is not None:
            key_padding_mask = key_padding_mask.unsqueeze(0)

    # set up shape vars
    tgt_len, bsz, embed_dim = query.shape
    src_len, _, _ = key.shape
    assert (
        embed_dim == embed_dim_to_check
    ), f"was expecting embedding dimension of {embed_dim_to_check}, but got {embed_dim}"
    if isinstance(embed_dim, torch.Tensor):
        # embed_dim can be a tensor when JIT tracing
        head_dim = embed_dim.div(num_heads, rounding_mode="trunc")
    else:
        head_dim = embed_dim // num_heads
    assert (
        head_dim * num_heads == embed_dim
    ), f"embed_dim {embed_dim} not divisible by num_heads {num_heads}"
    if use_separate_proj_weight:
        # allow MHA to have different embedding dimensions when separate projection weights are used
        assert (
            key.shape[:2] == value.shape[:2]
        ), f"key's sequence and batch dims {key.shape[:2]} do not match value's {value.shape[:2]}"
    else:
        assert (
            key.shape == value.shape
        ), f"key shape {key.shape} does not match value shape {value.shape}"

    #
    # compute in-projection
    #
    if not use_separate_proj_weight:
        q, k, v = _in_projection_packed(query, key, value, in_proj_weight, in_proj_bias)
    else:
        assert (
            q_proj_weight is not None
        ), "use_separate_proj_weight is True but q_proj_weight is None"
        assert (
            k_proj_weight is not None
        ), "use_separate_proj_weight is True but k_proj_weight is None"
        assert (
            v_proj_weight is not None
        ), "use_separate_proj_weight is True but v_proj_weight is None"
        if in_proj_bias is None:
            b_q = b_k = b_v = None
        else:
            b_q, b_k, b_v = in_proj_bias.chunk(3)
        q, k, v = _in_projection(
            query,
            key,
            value,
            q_proj_weight,
            k_proj_weight,
            v_proj_weight,
            b_q,
            b_k,
            b_v,
        )

    # prep attention mask
    if attn_mask is not None:
        if attn_mask.dtype == torch.uint8:
            warnings.warn(
                "Byte tensor for attn_mask in nn.MultiheadAttention is deprecated. Use bool tensor instead."
            )
            attn_mask = attn_mask.to(torch.bool)
        else:
            assert (
                attn_mask.is_floating_point() or attn_mask.dtype == torch.bool
            ), f"Only float, byte, and bool types are supported for attn_mask, not {attn_mask.dtype}"
        # ensure attn_mask's dim is 3
        if attn_mask.dim() == 2:
            correct_2d_size = (tgt_len, src_len)
            if attn_mask.shape != correct_2d_size:
                raise RuntimeError(
                    f"The shape of the 2D attn_mask is {attn_mask.shape}, but should be {correct_2d_size}."
                )
            attn_mask = attn_mask.unsqueeze(0)
        elif attn_mask.dim() == 3:
            correct_3d_size = (bsz * num_heads, tgt_len, src_len)
            if attn_mask.shape != correct_3d_size:
                raise RuntimeError(
                    f"The shape of the 3D attn_mask is {attn_mask.shape}, but should be {correct_3d_size}."
                )
        else:
            raise RuntimeError(
                f"attn_mask's dimension {attn_mask.dim()} is not supported"
            )

    # prep key padding mask
    if key_padding_mask is not None and key_padding_mask.dtype == torch.uint8:
        warnings.warn(
            "Byte tensor for key_padding_mask in nn.MultiheadAttention is deprecated. Use bool tensor instead."
        )
        key_padding_mask = key_padding_mask.to(torch.bool)

    # add bias along batch dimension (currently second)
    if bias_k is not None and bias_v is not None:
        assert static_k is None, "bias cannot be added to static key."
        assert static_v is None, "bias cannot be added to static value."
        k = torch.cat([k, bias_k.repeat(1, bsz, 1)])
        v = torch.cat([v, bias_v.repeat(1, bsz, 1)])
        if attn_mask is not None:
            attn_mask = torch.nn.functional.pad(attn_mask, (0, 1))
        if key_padding_mask is not None:
            key_padding_mask = torch.nn.functional.pad(key_padding_mask, (0, 1))
    else:
        assert bias_k is None
        assert bias_v is None

    #
    # reshape q, k, v for multihead attention and make em batch first
    #
    q = q.contiguous().view(tgt_len, bsz * num_heads, head_dim).transpose(0, 1)
    if static_k is None:
        k = k.contiguous().view(k.shape[0], bsz * num_heads, head_dim).transpose(0, 1)
    else:
        # TODO finish disentangling control flow so we don't do in-projections when statics are passed
        assert (
            static_k.size(0) == bsz * num_heads
        ), f"expecting static_k.size(0) of {bsz * num_heads}, but got {static_k.size(0)}"
        assert (
            static_k.size(2) == head_dim
        ), f"expecting static_k.size(2) of {head_dim}, but got {static_k.size(2)}"
        k = static_k
    if static_v is None:
        v = v.contiguous().view(v.shape[0], bsz * num_heads, head_dim).transpose(0, 1)
    else:
        # TODO finish disentangling control flow so we don't do in-projections when statics are passed
        assert (
            static_v.size(0) == bsz * num_heads
        ), f"expecting static_v.size(0) of {bsz * num_heads}, but got {static_v.size(0)}"
        assert (
            static_v.size(2) == head_dim
        ), f"expecting static_v.size(2) of {head_dim}, but got {static_v.size(2)}"
        v = static_v

    # add zero attention along batch dimension (now first)
    if add_zero_attn:
        zero_attn_shape = (bsz * num_heads, 1, head_dim)
        k = torch.cat(
            [k, torch.zeros(zero_attn_shape, dtype=k.dtype, device=k.device)], dim=1
        )
        v = torch.cat(
            [v, torch.zeros(zero_attn_shape, dtype=v.dtype, device=v.device)], dim=1
        )
        if attn_mask is not None:
            attn_mask = torch.nn.functional.pad(attn_mask, (0, 1))
        if key_padding_mask is not None:
            key_padding_mask = torch.nn.functional.pad(key_padding_mask, (0, 1))

    # update source sequence length after adjustments
    src_len = k.size(1)

    # merge key padding and attention masks
    if key_padding_mask is not None:
        assert key_padding_mask.shape == (
            bsz,
            src_len,
        ), f"expecting key_padding_mask shape of {(bsz, src_len)}, but got {key_padding_mask.shape}"
        key_padding_mask = (
            key_padding_mask.view(bsz, 1, 1, src_len)
            .expand(-1, num_heads, -1, -1)
            .reshape(bsz * num_heads, 1, src_len)
        )
        if attn_mask is None:
            attn_mask = key_padding_mask
        elif attn_mask.dtype == torch.bool:
            attn_mask = attn_mask.logical_or(key_padding_mask)
        else:
            attn_mask = attn_mask.masked_fill(key_padding_mask, attn_mask_value)

    # convert mask to float
    if attn_mask is not None and attn_mask.dtype == torch.bool:
        new_attn_mask = torch.zeros_like(attn_mask, dtype=q.dtype)
        new_attn_mask.masked_fill_(attn_mask, attn_mask_value)
        attn_mask = new_attn_mask

    # adjust dropout probability
    if not training:
        dropout_p = 0.0

    #
    # (deep breath) calculate attention and out projection
    #
    attn_output, attn_output_weights = attention_function(
        q,
        k,
        v,
        attn_mask,
        dropout_p,
        kernel_function=kernel_function,
        attn_mask_value=attn_mask_value,
        attn_masking_function=attn_masking_function,
        query_key_product=query_key_product,
    )
    attn_output = attn_output.transpose(0, 1).contiguous().view(tgt_len, bsz, embed_dim)
    attn_output = linear(attn_output, out_proj_weight, out_proj_bias)

    if need_weights:
        # optionally average attention weights over heads
        attn_output_weights = attn_output_weights.view(bsz, num_heads, tgt_len, src_len)
        if average_attn_weights:
            attn_output_weights = attn_output_weights.sum(dim=1) / num_heads

        if not is_batched:
            # squeeze the output if input was unbatched
            attn_output = attn_output.squeeze(1)
            attn_output_weights = attn_output_weights.squeeze(0)
        return attn_output, attn_output_weights
    else:
        if not is_batched:
            # squeeze the output if input was unbatched
            attn_output = attn_output.squeeze(1)
        return attn_output, None
