"""Adapted from https://github.com/vllm-project/vllm/blob/v0.6.4.post1/vllm/model_executor/parameter.py"""

import logging
from fractions import Fraction
from typing import Callable, Optional, Union

import torch
from torch.nn import Parameter

from sglang.srt.utils import is_cpu

__all__ = [
    "BasevLLMParameter",
    "PackedvLLMParameter",
    "PerTensorScaleParameter",
    "ModelWeightParameter",
    "ChannelQuantScaleParameter",
    "GroupQuantScaleParameter",
    "BlockQuantScaleParameter",
    "PackedColumnParameter",
    "RowvLLMParameter",
]

logger = logging.getLogger(__name__)

_is_cpu = is_cpu()


class BasevLLMParameter(Parameter):
    """
    Base parameter for vLLM linear layers. Extends the torch.nn.parameter
    by taking in a linear weight loader. Will copy the loaded weight
    into the parameter when the provided weight loader is called.
    """

    def __new__(cls, data: torch.Tensor, **kwargs):

        return super().__new__(cls, data=data, requires_grad=False)

    def __init__(self, data: torch.Tensor, weight_loader: Callable):
        """
        Initialize the BasevLLMParameter

        :param data: torch tensor with the parameter data
        :param weight_loader: weight loader callable

        :returns: a torch.nn.parameter
        """

        self._weight_loader = weight_loader

    @property
    def weight_loader(self):
        return self._weight_loader

    def _assert_and_load(self, loaded_weight: torch.Tensor):
        assert self.data.shape == loaded_weight.shape
        self.data.copy_(loaded_weight)

    def load_column_parallel_weight(self, loaded_weight: torch.Tensor):
        self._assert_and_load(loaded_weight)

    def load_row_parallel_weight(self, loaded_weight: torch.Tensor):
        self._assert_and_load(loaded_weight)

    def load_merged_column_weight(self, loaded_weight: torch.Tensor, **kwargs):
        self._assert_and_load(loaded_weight)

    def load_qkv_weight(self, loaded_weight: torch.Tensor, **kwargs):
        self._assert_and_load(loaded_weight)


class _ColumnvLLMParameter(BasevLLMParameter):
    """
    Private class defining weight loading functionality
    (load_merged_column_weight, load_qkv_weight)
    for parameters being loaded into linear layers with column
    parallelism. This includes QKV and MLP layers which are
    not already fused on disk. Requires an output dimension
    to be defined. Called within the weight loader of
    each of the column parallel linear layers.
    """

    def __init__(self, output_dim: int, **kwargs):
        self._output_dim = output_dim
        super().__init__(**kwargs)

    @property
    def output_dim(self):
        return self._output_dim

    def load_column_parallel_weight(
        self,
        loaded_weight: torch.Tensor,
        tp_rank: int,
        use_presharded_weights: bool = False,
    ):
        if not use_presharded_weights:
            shard_size = self.data.shape[self.output_dim]

            from sglang.srt.model_loader.weight_utils import (
                narrow_padded_param_and_loaded_weight,
            )

            if _is_cpu:
                param_data, loaded_weight = narrow_padded_param_and_loaded_weight(
                    self.data,
                    loaded_weight,
                    0,  # param_data_start
                    tp_rank * shard_size,
                    self.output_dim,
                    shard_size,
                )
                assert param_data.shape == loaded_weight.shape
                param_data.copy_(loaded_weight)
                return
            else:
                loaded_weight = loaded_weight.narrow(
                    self.output_dim, tp_rank * shard_size, shard_size
                )

        assert self.data.shape == loaded_weight.shape
        self.data.copy_(loaded_weight)

    def load_merged_column_weight(self, loaded_weight: torch.Tensor, **kwargs):

        shard_offset = kwargs.get("shard_offset")
        shard_size = kwargs.get("shard_size")
        tp_rank = kwargs.get("tp_rank")
        use_presharded_weights = kwargs.get("use_presharded_weights")
        if (
            isinstance(self, (PackedColumnParameter, PackedvLLMParameter))
            and self.packed_dim == self.output_dim
        ):
            shard_size, shard_offset = self.adjust_shard_indexes_for_packing(
                shard_offset=shard_offset, shard_size=shard_size
            )

        param_data = self.data

        param_data = param_data.narrow(self.output_dim, shard_offset, shard_size)

        from sglang.srt.model_loader.weight_utils import (
            narrow_padded_param_and_loaded_weight,
        )

        if _is_cpu:
            param_data, loaded_weight = narrow_padded_param_and_loaded_weight(
                param_data,
                loaded_weight,
                0,  # param_data_start
                tp_rank * shard_size,
                self.output_dim,
                shard_size,
                not use_presharded_weights,
            )
        else:
            if not use_presharded_weights:
                loaded_weight = loaded_weight.narrow(
                    self.output_dim, tp_rank * shard_size, shard_size
                )

        assert param_data.shape == loaded_weight.shape
        param_data.copy_(loaded_weight)

    def load_qkv_weight(
        self,
        loaded_weight: torch.Tensor,
        tp_rank: int,
        use_presharded_weights: bool = False,
        **kwargs,
    ):

        shard_offset = kwargs.get("shard_offset")
        shard_size = kwargs.get("shard_size")
        shard_id = kwargs.get("shard_id")
        num_heads = kwargs.get("num_heads")

        if (
            isinstance(self, (PackedColumnParameter, PackedvLLMParameter))
            and self.output_dim == self.packed_dim
        ):
            shard_size, shard_offset = self.adjust_shard_indexes_for_packing(
                shard_offset=shard_offset, shard_size=shard_size
            )

        param_data = self.data
        shard_id = tp_rank if shard_id == "q" else tp_rank // num_heads
        param_data = param_data.narrow(self.output_dim, shard_offset, shard_size)

        if _is_cpu:
            from sglang.srt.model_loader.weight_utils import (
                narrow_padded_param_and_loaded_weight,
            )

            param_data, loaded_weight = narrow_padded_param_and_loaded_weight(
                param_data,
                loaded_weight,
                0,  # param_data_start
                shard_id * shard_size,
                self.output_dim,
                shard_size,
                not use_presharded_weights,
            )
        else:
            if not use_presharded_weights:
                loaded_weight = loaded_weight.narrow(
                    self.output_dim, shard_id * shard_size, shard_size
                )

        assert (
            param_data.shape == loaded_weight.shape
        ), f"{param_data.shape=}, {loaded_weight.shape=}"
        param_data.copy_(loaded_weight)


class RowvLLMParameter(BasevLLMParameter):
    """
    Parameter class defining weight_loading functionality
    (load_row_parallel_weight) for parameters being loaded
    into linear layers with row parallel functionality.
    Requires an input_dim to be defined.
    """

    def __init__(self, input_dim: int, **kwargs):
        self._input_dim = input_dim
        super().__init__(**kwargs)

    @property
    def input_dim(self):
        return self._input_dim

    def load_row_parallel_weight(
        self,
        loaded_weight: torch.Tensor,
        tp_rank: int,
        use_presharded_weights: bool = False,
    ):
        if not use_presharded_weights:
            shard_size = self.data.shape[self.input_dim]

            from sglang.srt.model_loader.weight_utils import (
                narrow_padded_param_and_loaded_weight,
            )

            if _is_cpu:
                param_data, loaded_weight = narrow_padded_param_and_loaded_weight(
                    self.data,
                    loaded_weight,
                    0,  # param_data_start
                    tp_rank * shard_size,
                    self.input_dim,
                    shard_size,
                )

                assert param_data.shape == loaded_weight.shape
                param_data.copy_(loaded_weight)

                return
            else:
                loaded_weight = loaded_weight.narrow(
                    self.input_dim, tp_rank * shard_size, shard_size
                )

        if len(loaded_weight.shape) == 0:
            loaded_weight = loaded_weight.reshape(1)

        assert self.data.shape == loaded_weight.shape
        self.data.copy_(loaded_weight)


class ModelWeightParameter(_ColumnvLLMParameter, RowvLLMParameter):
    """
    Parameter class for linear layer weights. Uses both column and
    row parallelism.
    """

    pass


class GroupQuantScaleParameter(_ColumnvLLMParameter, RowvLLMParameter):
    """
    Parameter class for weight scales loaded for weights with
    grouped quantization. Uses both column and row parallelism.
    """

    pass


class ChannelQuantScaleParameter(_ColumnvLLMParameter):
    """
    Parameter class for weight scales loaded for weights with
    channel-wise quantization. Equivalent to _ColumnvLLMParameter.
    """

    pass


class BlockQuantScaleParameter(_ColumnvLLMParameter, RowvLLMParameter):
    """
    Parameter class for weight scales loaded for weights with
    block-wise quantization. Uses both column and row parallelism.
    """

    pass


class PerTensorScaleParameter(BasevLLMParameter):
    """
    Parameter class for scales where the number of scales is
    equivalent to the number of logical matrices in fused linear
    layers (e.g. for QKV, there are 3 scales loaded from disk).
    This is relevant to weights with per-tensor quantization.
    Adds functionality to map the scalers to a shard during
    weight loading.

    Note: additional parameter manipulation may be handled
    for each quantization config specifically, within
    process_weights_after_loading
    """

    def __init__(self, **kwargs):
        self.qkv_idxs = {"q": 0, "k": 1, "v": 2}
        super().__init__(**kwargs)

    def _shard_id_as_int(self, shard_id: Union[str, int]) -> int:
        if isinstance(shard_id, int):
            return shard_id

        # if not int, assume shard_id for qkv
        # map to int and return
        assert isinstance(shard_id, str)
        assert shard_id in self.qkv_idxs
        return self.qkv_idxs[shard_id]

    # For row parallel layers, no sharding needed
    # load weight into parameter as is
    def load_row_parallel_weight(self, *args, **kwargs):
        kwargs.pop("tp_rank", None)
        kwargs.pop("use_presharded_weights", None)
        super().load_row_parallel_weight(*args, **kwargs)

    def load_merged_column_weight(self, *args, **kwargs):
        self._load_into_shard_id(*args, **kwargs)

    def load_qkv_weight(self, *args, **kwargs):
        self._load_into_shard_id(*args, **kwargs)

    def load_column_parallel_weight(self, *args, **kwargs):
        kwargs.pop("tp_rank", None)
        kwargs.pop("use_presharded_weights", None)
        super().load_row_parallel_weight(*args, **kwargs)

    def _load_into_shard_id(
        self, loaded_weight: torch.Tensor, shard_id: Union[str, int], **kwargs
    ):
        """
        Slice the parameter data based on the shard id for
        loading.
        """

        param_data = self.data
        shard_id = self._shard_id_as_int(shard_id)

        # AutoFP8 scales do not have a shape
        # compressed-tensors scales do have a shape
        if len(loaded_weight.shape) != 0:
            assert loaded_weight.shape[0] == 1
            loaded_weight = loaded_weight[0]

        param_data = param_data[shard_id]
        assert param_data.shape == loaded_weight.shape
        param_data.copy_(loaded_weight)


class PackedColumnParameter(_ColumnvLLMParameter):
    """
    Parameter for model parameters which are packed on disk
    and support column parallelism only. See PackedvLLMParameter
    for more details on the packed properties.
    """

    def __init__(
        self,
        packed_factor: Union[int, Fraction],
        packed_dim: int,
        marlin_tile_size: Optional[int] = None,
        **kwargs,
    ):
        self._packed_factor = packed_factor
        self._packed_dim = packed_dim
        self._marlin_tile_size = marlin_tile_size
        super().__init__(**kwargs)

    @property
    def packed_dim(self):
        return self._packed_dim

    @property
    def packed_factor(self):
        return self._packed_factor

    @property
    def marlin_tile_size(self):
        return self._marlin_tile_size

    def adjust_shard_indexes_for_packing(self, shard_size, shard_offset):
        return _adjust_shard_indexes_for_packing(
            shard_size=shard_size,
            shard_offset=shard_offset,
            packed_factor=self.packed_factor,
            marlin_tile_size=self.marlin_tile_size,
        )


class PackedvLLMParameter(ModelWeightParameter):
    """
    Parameter for model weights which are packed on disk.
    Example: GPTQ Marlin weights are int4 or int8, packed into int32.
    Extends the ModelWeightParameter to take in the
    packed factor, the packed dimension, and optionally, marlin
    tile size for marlin kernels. Adjusts the shard_size and
    shard_offset for fused linear layers model weight loading
    by accounting for packing and optionally, marlin tile size.
    """

    def __init__(
        self,
        packed_factor: Union[int, Fraction],
        packed_dim: int,
        marlin_tile_size: Optional[int] = None,
        **kwargs,
    ):
        self._packed_factor = packed_factor
        self._packed_dim = packed_dim
        self._marlin_tile_size = marlin_tile_size
        super().__init__(**kwargs)

    @property
    def packed_dim(self):
        return self._packed_dim

    @property
    def packed_factor(self):
        return self._packed_factor

    @property
    def marlin_tile_size(self):
        return self._marlin_tile_size

    def adjust_shard_indexes_for_packing(self, shard_size, shard_offset):
        return _adjust_shard_indexes_for_packing(
            shard_size=shard_size,
            shard_offset=shard_offset,
            packed_factor=self.packed_factor,
            marlin_tile_size=self.marlin_tile_size,
        )


def permute_param_layout_(
    param: BasevLLMParameter, input_dim: int, output_dim: int, **kwargs
) -> BasevLLMParameter:
    """
    Permute a parameter's layout to the specified input and output dimensions,
    useful for forcing the parameter into a known layout, for example, if I need
    a packed (quantized) weight matrix to be in the layout
        {input_dim = 0, output_dim = 1, packed_dim = 0}
    then I can call:
        permute_param_layout_(x, input_dim=0, output_dim=1, packed_dim=0)
    to ensure x is in the correct layout (permuting it to the correct layout if
    required, asserting if it cannot get it to the correct layout)
    """

    curr_input_dim = getattr(param, "input_dim", None)
    curr_output_dim = getattr(param, "output_dim", None)

    if curr_input_dim is None or curr_output_dim is None:
        assert param.data.dim() == 2, (
            "permute_param_layout_ only supports 2D parameters when either "
            "input_dim or output_dim is not set"
        )

    # if one of the dimensions is not set, set it to the opposite of the other
    #  we can only do this since we asserted the parameter is 2D above
    if curr_input_dim is None:
        assert curr_output_dim is not None, "either input or output dim must be set"
        curr_input_dim = (curr_output_dim + 1) % 2
    if curr_output_dim is None:
        assert curr_input_dim is not None, "either input or output dim must be set"
        curr_output_dim = (curr_input_dim + 1) % 2

    # create permutation from the current layout to the layout with
    # self.input_dim at input_dim and self.output_dim at output_dim preserving
    # other dimensions
    perm = [
        i for i in range(param.data.dim()) if i not in [curr_input_dim, curr_output_dim]
    ]
    perm.insert(input_dim, curr_input_dim)
    perm.insert(output_dim, curr_output_dim)

    if "packed_dim" in kwargs:
        assert (
            hasattr(param, "packed_dim")
            and param.packed_dim == perm[kwargs["packed_dim"]]
        ), "permute_param_layout_ currently doesn't support repacking"

    param.data = param.data.permute(*perm)
    if hasattr(param, "_input_dim"):
        param._input_dim = input_dim
    if hasattr(param, "_output_dim"):
        param._output_dim = output_dim
    if "packed_dim" in kwargs and hasattr(param, "_packed_dim"):
        param._packed_dim = kwargs["packed_dim"]

    return param


def _adjust_shard_indexes_for_marlin(shard_size, shard_offset, marlin_tile_size):
    return shard_size * marlin_tile_size, shard_offset * marlin_tile_size


def _adjust_shard_indexes_for_packing(
    shard_size, shard_offset, packed_factor, marlin_tile_size
):
    shard_size = shard_size // packed_factor
    shard_offset = shard_offset // packed_factor
    if marlin_tile_size is not None:
        return _adjust_shard_indexes_for_marlin(
            shard_size=shard_size,
            shard_offset=shard_offset,
            marlin_tile_size=marlin_tile_size,
        )
    return shard_size, shard_offset
