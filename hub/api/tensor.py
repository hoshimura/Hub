from hub.util.keys import tensor_exists
from hub.core.sample import Sample  # type: ignore
from typing import List, Sequence, Union, Optional, Tuple, Dict
from hub.util.shape import ShapeInterval

import numpy as np

from hub.core.chunk_engine import ChunkEngine, SampleValue
from hub.core.storage import LRUCache
from hub.util.exceptions import TensorDoesNotExistError, InvalidKeyTypeError
from hub.core.index import Index


class Tensor:
    def __init__(
        self,
        key: str,
        storage: LRUCache,
        index: Optional[Index] = None,
    ):
        """Initializes a new tensor.

        Note:
            This operation does not create a new tensor in the storage provider,
            and should normally only be performed by Hub internals.

        Args:
            key (str): The internal identifier for this tensor.
            storage (LRUCache): The storage provider for the parent dataset.
            index: The Index object restricting the view of this tensor.
                Can be an int, slice, or (used internally) an Index object.

        Raises:
            TensorDoesNotExistError: If no tensor with `key` exists and a `tensor_meta` was not provided.
        """

        self.key = key
        self.storage = storage
        self.index = index or Index()

        if not tensor_exists(self.key, self.storage):
            raise TensorDoesNotExistError(self.key)

        self.chunk_engine = ChunkEngine(self.key, self.storage)

        self._sample: Optional[Tuple(int, int)] = None

    def extend(self, samples: Union[np.ndarray, Sequence[SampleValue]]):
        """Extends the end of the tensor by appending multiple elements from a sequence. Accepts a sequence, a single batched numpy array,
        or a sequence of `hub.load` outputs, which can be used to load files. See examples down below.

        Example:
            numpy input:
                >>> len(tensor)
                0
                >>> tensor.extend(np.zeros((100, 28, 28, 1)))
                >>> len(tensor)
                100

            file input:
                >>> len(tensor)
                0
                >>> tensor.extend([
                        hub.load("path/to/image1"),
                        hub.load("path/to/image2"),
                    ])
                >>> len(tensor)
                2


        Args:
            samples (np.ndarray, Sequence, Sequence[Sample]): The data to add to the tensor.
                The length should be equal to the number of samples to add.
        """
        self.chunk_engine.extend(samples)
        self._sample = None

    def append(
        self,
        sample: Union[np.ndarray, float, int, Sample],
    ):
        """Appends a single sample to the end of the tensor. Can be an array, scalar value, or the return value from `hub.load`,
        which can be used to load files. See examples down below.

        Examples:
            numpy input:
                >>> len(tensor)
                0
                >>> tensor.append(np.zeros((28, 28, 1)))
                >>> len(tensor)
                1

            file input:
                >>> len(tensor)
                0
                >>> tensor.append(hub.load("path/to/file"))
                >>> len(tensor)
                1

        Args:
            sample (np.ndarray, float, int, Sample): The data to append to the tensor. `Sample` is generated by `hub.load`. See the above examples.
        """
        self.extend([sample])

    @property
    def meta(self):
        return self.chunk_engine.tensor_meta

    @property
    def shape(self) -> Tuple[Optional[int], ...]:
        """Get the shape of this tensor. Length is included.

        Note:
            If you don't want `None` in the output shape or want the lower/upper bound shapes,
            use `tensor.shape_interval` instead.

        Example:
            >>> tensor.append(np.zeros((10, 10)))
            >>> tensor.append(np.zeros((10, 15)))
            >>> tensor.shape
            (2, 10, None)

        Returns:
            tuple: Tuple where each value is either `None` (if that axis is dynamic) or
                an `int` (if that axis is fixed).
        """

        return self.shape_interval.astuple()

    @property
    def dtype(self) -> np.dtype:
        if self.meta.dtype:
            return np.dtype(self.meta.dtype)
        return None

    @property
    def shape_interval(self) -> ShapeInterval:
        """Returns a `ShapeInterval` object that describes this tensor's shape more accurately. Length is included.

        Note:
            If you are expecting a `tuple`, use `tensor.shape` instead.

        Example:
            >>> tensor.append(np.zeros((10, 10)))
            >>> tensor.append(np.zeros((10, 15)))
            >>> tensor.shape_interval
            ShapeInterval(lower=(2, 10, 10), upper=(2, 10, 15))
            >>> str(tensor.shape_interval)
            (2, 10, 10:15)

        Returns:
            ShapeInterval: Object containing `lower` and `upper` properties.
        """

        length = [len(self)]

        min_shape = length + list(self.meta.min_shape)
        max_shape = length + list(self.meta.max_shape)

        return ShapeInterval(min_shape, max_shape)

    @property
    def is_dynamic(self) -> bool:
        """Will return True if samples in this tensor have shapes that are unequal."""
        return self.shape_interval.is_dynamic

    def __len__(self):
        """Returns the length of the primary axis of the tensor.
        Accounts for indexing into the tensor object.

        Examples:
            >>> len(tensor)
            0
            >>> tensor.extend(np.zeros((100, 10, 10)))
            >>> len(tensor)
            100
            >>> len(tensor[5:10])
            5

        Returns:
            int: The current length of this tensor.
        """

        return self.index.length(self.chunk_engine.num_samples)

    def __getitem__(
        self,
        item: Union[int, slice, List[int], Tuple[Union[int, slice, Tuple[int]]], Index],
    ):
        if not isinstance(item, (int, slice, list, tuple, Index)):
            raise InvalidKeyTypeError(item)
        return Tensor(self.key, self.storage, index=self.index[item])

    def __setitem__(self, item: Union[int, slice], value: np.ndarray):
        raise NotImplementedError("Tensor update not currently supported!")

    def __iter__(self):
        for i, (chunk_id, local_sample_index) in enumerate(
            self.chunk_engine.chunk_id_encoder.iter(self.index.values[0].value)
        ):
            tensor_i = Tensor(self.key, self.storage, index=self.index[i])
            tensor_i._sample = chunk_id, local_sample_index
            yield tensor_i

    def numpy(self, aslist=False) -> Union[np.ndarray, List[np.ndarray]]:
        """Computes the contents of the tensor in numpy format.

        Args:
            aslist (bool): If True, a list of np.ndarrays will be returned. Helpful for dynamic tensors.
                If False, a single np.ndarray will be returned unless the samples are dynamically shaped, in which case
                an error is raised.

        Raises:
            DynamicTensorNumpyError: If reading a dynamically-shaped array slice without `aslist=True`.

        Returns:
            A numpy array containing the data represented by this tensor.
        """
        if self._sample:
            chunk_id, local_sample_index = self._sample
            chunk = self.chunk_engine.get_chunk_from_id(chunk_id)
            return self.chunk_engine.read_sample_from_chunk(chunk, local_sample_index)

        return self.chunk_engine.numpy(self.index, aslist=aslist)

    def __str__(self):
        index_str = f", index={self.index}"
        if self.index.is_trivial():
            index_str = ""
        return f"Tensor(key={repr(self.key)}{index_str})"

    __repr__ = __str__
