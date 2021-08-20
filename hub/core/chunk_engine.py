from hub.core.fast_forwarding import ffw_chunk_id_encoder
import warnings
from hub.util.casting import get_dtype
from hub.core.compression import decompress_array
from math import ceil
from typing import Any, Optional, Sequence, Union, Tuple, List, Set
from hub.util.exceptions import (
    CorruptedMetaError,
    DynamicTensorNumpyError,
    UpdateSampleError,
)
from hub.core.meta.tensor_meta import TensorMeta
from hub.core.index.index import Index
from hub.util.keys import (
    get_chunk_key,
    get_chunk_id_encoder_key,
    get_tensor_meta_key,
)
from hub.core.sample import Sample, SampleValue  # type: ignore
from hub.constants import DEFAULT_MAX_CHUNK_SIZE

import numpy as np

from hub.core.storage.lru_cache import LRUCache

from hub.core.chunk import Chunk

from hub.core.meta.encode.chunk_id import ChunkIdEncoder

from hub.core.serialize import serialize_input_samples


def is_uniform_sequence(samples):
    """Determines if a sequence of samples has uniform type and shape, allowing it to be vectorized by `ChunkEngine.extend`."""
    if len(set(map(type, samples))) != 1:
        # Cannot vectorize sequence with inconsistent types
        return False
    elif any(isinstance(s, np.ndarray) for s in samples):
        # Numpy arrays will only be vectorized if they have the same shape
        return len(set(s.shape for s in samples)) == 1
    elif any(isinstance(s, Sample) for s in samples):
        # Sample objects will not be vectorized
        return False
    else:
        # Scalar samples can be vectorized
        return True


# used for warning the user if updating a tensor caused suboptimal chunks
CHUNK_UPDATE_WARN_PORTION = 0.2


class ChunkEngine:
    def __init__(
        self,
        key: str,
        cache: LRUCache,
        meta_cache: LRUCache = None,
    ):
        """Handles creating `Chunk`s and filling them with incoming samples.

        Data delegation:
            All samples must live inside a chunk. No chunks may contain partial samples, only 1 chunk per sample.
            A chunk holds the dynamic information for the samples they contain (like shape and byte ranges).
            For more information on the `Chunk` format, check out the `Chunk` class.

        ChunkIdEncoder:
            The `ChunkIdEncoder` bidirectionally maps samples to the chunk IDs they live in. For more information,
            see `ChunkIdEncoder`'s docstring.

        Example:
            Given:
                Sample sizes: [1 * MB, 1 * MB, 14 * MB, 15 * MB, 15 * MB]
                Min chunk size: 16 * MB
                Max chunk size: 32 * MB


            Basic logic:
                >>> chunks = []
                >>> chunks.append(sum([1 * MB, 1 * MB, 14 * MB, 15 * MB]))  # i=(0, 1, 2, 3)
                >>> chunks[-1]
                31 * MB
                >>> chunks.append(sum([15 * MB]))  # i=(4,)
                >>> chunks[-1]
                15 * MB

            Samples 0, 1, 2, and 3 can be stored in 1 chunk. sample 4 resides in it's own chunk.

            If more samples come later: sizes = [15 * MB, 1 * MB]

            Basic logic:
                >>> len(chunks)
                2
                >>> chunks[-1]
                15 * MB
                >>> chunks[-1] += sum([15 * MB, 1 * MB])  # i=(5, 6)
                >>> chunks[-1]
                31 * MB
                >>> sum(chunks)
                62 * MB
                >>> len(chunks)
                2

            Because our max chunk size is 32 * MB, we try to fit as much data into this size as possible.


        Args:
            key (str): Tensor key.
            cache (LRUCache): Cache for which chunks and the metadata are stored.
            meta_cache (LRUCache): Cache used for storing non chunk data such as tensor meta and chunk id encoder during transforms in memory.

        Raises:
            ValueError: If invalid max chunk size.
        """

        self.key = key
        self.cache = cache
        self._meta_cache = meta_cache

    @property
    def max_chunk_size(self):
        # no chunks may exceed this
        return (
            getattr(self.tensor_meta, "max_chunk_size", None) or DEFAULT_MAX_CHUNK_SIZE
        )

    @property
    def min_chunk_size(self):
        # only the last chunk may be less than this
        return self.max_chunk_size // 2

    @property
    def meta_cache(self) -> LRUCache:
        return self._meta_cache or self.cache

    @property
    def chunk_id_encoder(self) -> ChunkIdEncoder:
        """Gets the chunk id encoder from cache, if one is not found it creates a blank encoder.
        For more information on what `ChunkIdEncoder` is used for, see the `__init__` docstring.

        Raises:
            CorruptedMetaError: If chunk id encoding was corrupted.

        Returns:
            ChunkIdEncoder: The chunk ID encoder handles the mapping between sample indices
                and their corresponding chunks.
        """
        key = get_chunk_id_encoder_key(self.key)
        if not self.chunk_id_encoder_exists:

            # 1 because we always update the meta information before writing the samples (to account for potentially corrupted data in the future)
            if self.tensor_meta.length > 1:
                raise CorruptedMetaError(
                    f"Tensor length is {self.tensor_meta.length}, but could not find the chunk id encoder."
                )

            enc = ChunkIdEncoder()
            self.meta_cache[key] = enc
            return enc

        enc = self.meta_cache.get_cachable(key, ChunkIdEncoder)
        return enc

    @property
    def chunk_id_encoder_exists(self) -> bool:
        try:
            key = get_chunk_id_encoder_key(self.key)
            self.meta_cache[key]
            return True
        except KeyError:
            return False

    @property
    def num_chunks(self) -> int:
        if not self.chunk_id_encoder_exists:
            return 0
        return self.chunk_id_encoder.num_chunks

    @property
    def num_samples(self) -> int:
        if not self.chunk_id_encoder_exists:
            return 0
        return self.chunk_id_encoder.num_samples

    @property
    def last_chunk(self) -> Optional[Chunk]:
        if self.num_chunks == 0:
            return None

        return self.get_chunk(self.last_chunk_key)

    def get_chunk(self, chunk_key: str) -> Chunk:
        return self.cache.get_cachable(chunk_key, Chunk)

    @property
    def last_chunk_key(self) -> str:
        last_chunk_name = self.chunk_id_encoder.get_name_for_chunk(-1)
        last_chunk_key = get_chunk_key(self.key, last_chunk_name)
        return last_chunk_key

    @property
    def tensor_meta(self):
        tensor_meta_key = get_tensor_meta_key(self.key)
        return self.meta_cache.get_cachable(tensor_meta_key, TensorMeta)

    def _append_bytes(self, buffer: memoryview, shape: Tuple[int]):
        """Treat `buffer` as a single sample and place them into `Chunk`s. This function implements the algorithm for
        determining which chunks contain which parts of `buffer`.

        Args:
            buffer (memoryview): Buffer that represents a single sample. Can have a
                length of 0, in which case `shape` should contain at least one 0 (empty sample).
            shape (Tuple[int]): Shape for the sample that `buffer` represents.
        """

        # num samples is always 1 when appending
        num_samples = 1

        buffer_consumed = self._try_appending_to_last_chunk(buffer, shape)
        if not buffer_consumed:
            self._append_to_new_chunk(buffer, shape)

        self.chunk_id_encoder.register_samples(num_samples)
        self._synchronize_cache()

    def _synchronize_cache(self, chunk_keys: List[str] = None):
        """Synchronizes cachables with the cache.

        Args:
            chunk_keys (List[str]): List of chunk keys to be synchronized. If None, only the last chunk will be synchronized. Defaults to None.
        """

        # TODO implement tests for cache size compute
        # TODO: optimize this by storing all of these keys in the chunk engine's state (posixpath.joins are pretty slow)

        # synchronize chunks
        if chunk_keys is None:
            chunk_keys = [self.last_chunk_key]
        for chunk_key in chunk_keys:
            chunk = self.get_chunk(chunk_key)
            self.cache.update_used_cache_for_path(chunk_key, chunk.nbytes)  # type: ignore

        # synchronize tensor meta
        tensor_meta_key = get_tensor_meta_key(self.key)
        self.meta_cache[tensor_meta_key] = self.tensor_meta

        # synchronize chunk ID encoder
        chunk_id_key = get_chunk_id_encoder_key(self.key)
        self.meta_cache[chunk_id_key] = self.chunk_id_encoder

    def _try_appending_to_last_chunk(
        self, buffer: memoryview, shape: Tuple[int]
    ) -> bool:
        """Will store `buffer` inside of the last chunk if it can.
        It can be stored in the last chunk if it exists and has space for `buffer`.

        Args:
            buffer (memoryview): Data to store. This can represent any number of samples.
            shape (Tuple[int]): Shape for the sample that `buffer` represents.

        Returns:
            bool: True if `buffer` was successfully written to the last chunk, otherwise False.
        """

        last_chunk = self.last_chunk
        if last_chunk is None:
            return False

        incoming_num_bytes = len(buffer)

        if last_chunk.is_under_min_space(self.min_chunk_size):
            last_chunk_size = last_chunk.num_data_bytes
            chunk_ct_content = _min_chunk_ct_for_data_size(
                self.max_chunk_size, incoming_num_bytes
            )

            extra_bytes = min(incoming_num_bytes, self.max_chunk_size - last_chunk_size)
            combined_chunk_ct = _min_chunk_ct_for_data_size(
                self.max_chunk_size, incoming_num_bytes + last_chunk_size
            )

            # combine if count is same
            if combined_chunk_ct == chunk_ct_content:
                last_chunk.append_sample(
                    buffer[:extra_bytes], self.max_chunk_size, shape
                )
                return True

        return False

    def _append_to_new_chunk(self, buffer: memoryview, shape: Tuple[int]):
        """Will create a new chunk and store `buffer` inside of it. Assumes that `buffer`'s length is < max chunk size.
        This should be called if `buffer` could not be added to the last chunk.

        Args:
            buffer (memoryview): Data to store. This can represent any number of samples.
            shape (Tuple[int]): Shape for the sample that `buffer` represents.
        """

        # check if `last_chunk_extended` to handle empty samples
        new_chunk = self._create_new_chunk()
        new_chunk.append_sample(buffer, self.max_chunk_size, shape)

    def _create_new_chunk(self):
        """Creates and returns a new `Chunk`. Automatically creates an ID for it and puts a reference in the cache."""

        chunk_id = self.chunk_id_encoder.generate_chunk_id()
        chunk = Chunk()
        chunk_name = ChunkIdEncoder.name_from_id(chunk_id)
        chunk_key = get_chunk_key(self.key, chunk_name)
        self.cache[chunk_key] = chunk
        return chunk

    def extend(self, samples: Union[np.ndarray, Sequence[SampleValue]]):
        """Formats a batch of `samples` and feeds them into `_append_bytes`."""

        self.cache.check_readonly()
        ffw_chunk_id_encoder(self.chunk_id_encoder)

        tensor_meta = self.tensor_meta
        if tensor_meta.dtype is None:
            tensor_meta.set_dtype(get_dtype(samples))

        samples = serialize_input_samples(samples, tensor_meta, self.min_chunk_size)
        for buffer, shape in samples:
            # update tensor meta length first because erroneous meta information is better than un-accounted for data.
            # TODO: move these functions somewhere usable by update and any other methods
            tensor_meta.update_shape_interval(shape)
            tensor_meta.length += 1
            self._append_bytes(buffer, shape)

        self.cache.maybe_flush()

    def append(self, sample: SampleValue):
        """Formats a single `sample` (compresseses/decompresses if applicable) and feeds it into `_append_bytes`."""
        self.extend([sample])

    def update(self, index: Index, samples: Union[Sequence[SampleValue], SampleValue]):
        """Update data at `index` with `samples`."""

        self.cache.check_readonly()
        ffw_chunk_id_encoder(self.chunk_id_encoder)

        if index.is_single_dim_effective():
            self._update_sample(index, samples)
        else:
            self._update_sample_subslice(index, samples)


    def _update_sample(self, index: Index, samples: Union[Sequence[SampleValue], SampleValue]):
        # TODO: docstring

        tensor_meta = self.tensor_meta
        enc = self.chunk_id_encoder
        updated_chunks = set()

        index_length = index.length(self.num_samples)
        samples = _make_sequence(samples, index_length)
        serialized_input_samples = serialize_input_samples(
            samples, tensor_meta, self.min_chunk_size
        )

        chunks_nbytes_after_updates = []
        global_sample_indices = tuple(index.values[0].indices(self.num_samples))
        for i, (buffer, shape) in enumerate(serialized_input_samples):
            global_sample_index = global_sample_indices[i]  # TODO!

            chunk = self.get_chunk_for_sample(global_sample_index, enc)

            local_sample_index = enc.translate_index_relative_to_chunks(
                global_sample_index
            )

            tensor_meta.update_shape_interval(shape)
            chunk.update_sample(local_sample_index, buffer, shape)
            updated_chunks.add(chunk)

            # only care about deltas if it isn't the last chunk
            if chunk.key != self.last_chunk_key:  # type: ignore
                chunks_nbytes_after_updates.append(chunk.nbytes)

        # TODO: [refactor] this is a hacky way, also `self._synchronize_cache` might be redundant. maybe chunks should use callbacks.
        for chunk in updated_chunks:
            self.cache[chunk.key] = chunk  # type: ignore

        self._synchronize_cache(chunk_keys=[])
        self.cache.maybe_flush()

        _warn_if_suboptimal_chunks(
            chunks_nbytes_after_updates, self.min_chunk_size, self.max_chunk_size
        )    

    def _update_sample_subslice(self, index: Index, samples: Union[Sequence[SampleValue], SampleValue]):
        # TODO: docstring

        if not hasattr(samples, "shape"):
            raise UpdateSampleError(f"Can only update a tensor subslice if the incoming data is a numpy array. Incoming type: {type(samples)}")

        raise NotImplementedError
    
    def numpy(
        self, index: Index, aslist: bool = False
    ) -> Union[np.ndarray, Sequence[np.ndarray]]:
        """Reads samples from chunks and returns as a numpy array. If `aslist=True`, returns a sequence of numpy arrays.

        Args:
            index (Index): Represents the samples to read from chunks. See `Index` for more information.
            aslist (bool): If True, the samples will be returned as a list of numpy arrays. If False, returns a single numpy array. Defaults to False.

        Raises:
            DynamicTensorNumpyError: If shapes of the samples being read are not all the same.

        Returns:
            Union[np.ndarray, Sequence[np.ndarray]]: Either a list of numpy arrays or a single numpy array (depending on the `aslist` argument).
        """

        length = self.num_samples
        enc = self.chunk_id_encoder
        last_shape = None
        samples = []

        for global_sample_index in index.values[0].indices(length):
            chunk = self.get_chunk_for_sample(global_sample_index, enc)
            sample = self.read_sample_from_chunk(global_sample_index, chunk)
            shape = sample.shape

            if not aslist and last_shape is not None:
                if shape != last_shape:
                    raise DynamicTensorNumpyError(self.key, index, "shape")

            samples.append(sample)
            last_shape = shape

        return _format_read_samples(samples, index, aslist)

    def get_chunk_for_sample(
        self, global_sample_index: int, enc: ChunkIdEncoder
    ) -> Chunk:
        """Retrives the `Chunk` object corresponding to `global_sample_index`.
        Args:
            global_sample_index (int): Index relative to the entire tensor representing the sample.
            enc (ChunkIdEncoder): Chunk ID encoder. This is an argument because right now it is
                sub-optimal to use `self.chunk_id_encoder` due to posixpath joins.
        Returns:
            Chunk: Chunk object that contains `global_sample_index`.
        """

        chunk_id = enc[global_sample_index]
        chunk_name = ChunkIdEncoder.name_from_id(chunk_id)
        chunk_key = get_chunk_key(self.key, chunk_name)
        chunk = self.cache.get_cachable(chunk_key, Chunk)
        chunk.key = chunk_key

        return chunk

    def read_sample_from_chunk(
        self, global_sample_index: int, chunk: Chunk, cast: bool = True
    ) -> np.ndarray:
        """Read a sample from a chunk, converts the global index into a local index. Handles decompressing if applicable."""

        expect_compressed = self.tensor_meta.sample_compression is not None
        dtype = self.tensor_meta.dtype

        enc = self.chunk_id_encoder

        buffer = chunk.memoryview_data
        local_sample_index = enc.translate_index_relative_to_chunks(global_sample_index)
        shape = chunk.shapes_encoder[local_sample_index]
        sb, eb = chunk.byte_positions_encoder[local_sample_index]

        buffer = buffer[sb:eb]

        if len(buffer) == 0:
            return np.zeros(shape, dtype=dtype)

        if expect_compressed:
            sample = decompress_array(buffer, shape)
            if cast and sample.dtype != self.tensor_meta.dtype:
                sample = sample.astype(self.tensor_meta.dtype)
        else:
            sample = np.frombuffer(buffer, dtype=dtype).reshape(shape)

        return sample

    def get_chunk_names(
        self, sample_index: int, last_index: int, target_chunk_count: int
    ) -> Set[str]:
        """Fetches a set of chunk names in which data starting from sample_index is contained.
            This is used by Pytorch integration.

        Args:
            sample_index: The index starting from which chunk names need to be fetched.
            last_index: The last index till which chunk names need to be fetched.
            target_chunk_count: The target number of chunk names required. The actual size of the returned set may be:-
                a) Less than target_chunk_count: If there are no more chunks to fetch.
                b) More than target_chunk_count: If the last chunk filling up target_chunk_count is a partial chunk, the remaining chunks are fetched.
                c) Equal to the target_chunk_count: In all other cases.

        Returns:
            Set of chunk names.
        """
        chunk_names: Set[str] = set()
        while len(chunk_names) < target_chunk_count and sample_index < last_index:
            chunk_id = self.chunk_id_encoder[sample_index]
            chunk = self.chunk_id_encoder.name_from_id(chunk_id)
            # todo, change to chunk_names.update once chunks returns sequence instead of single string
            chunk_names.add(chunk)
            sample_index += 1
        return chunk_names

    def validate_num_samples_is_synchronized(self):
        """Check if tensor meta length and chunk ID encoder are representing the same number of samples.
        Helpful for determining if a user has tampered with the tensor meta or the chunk ID encoder, or if
        the tensor was corruptd.

        Raises:
            CorruptedMetaError: tensor_meta and chunk_id_encoder must have the same num samples.
        """

        tensor_meta_length = self.tensor_meta.length

        # compare chunk ID encoder and tensor meta
        chunk_id_num_samples = (
            self.chunk_id_encoder.num_samples if self.chunk_id_encoder_exists else 0
        )
        if tensor_meta_length != chunk_id_num_samples:
            tkey = get_tensor_meta_key(self.key)
            ikey = get_chunk_id_encoder_key(self.key)
            raise CorruptedMetaError(
                f"'{tkey}' and '{ikey}' have a record of different numbers of samples. Got {tensor_meta_length} and {chunk_id_num_samples} respectively."
            )


def _format_read_samples(
    samples: Sequence[np.array], index: Index, aslist: bool
) -> Union[np.ndarray, List[np.ndarray]]:
    """Prepares samples being read from the chunk engine in the format the user expects."""

    samples = index.apply(samples)  # type: ignore

    if aslist and all(map(np.isscalar, samples)):
        samples = list(arr.item() for arr in samples)

    samples = index.apply_squeeze(samples)  # type: ignore

    if aslist:
        return samples
    else:
        return np.array(samples)


def _min_chunk_ct_for_data_size(chunk_max_data_bytes: int, size: int) -> int:
    """Calculates the minimum number of chunks in which data of given size can be fit."""
    return ceil(size / chunk_max_data_bytes)


def _make_sequence(
    samples: Union[Sequence[SampleValue], SampleValue], index_length: int
) -> Sequence[SampleValue]:
    """Make `samples` a sequence of `SampleValue`s.

    Args:
        samples (Union[Sequence[SampleValue], SampleValue]): Incoming samples to be made into a sequence.
        index_length (int): Number of expected samples in the sequence.

    Raises:
        ValueError: If `index_length` is incompatible with the true length of `samples`.

    Returns:
        Sequence[SampleValue]: Sequence of `SampleValue`s with the same length as `index_length`.
    """

    if index_length == 1:
        if hasattr(samples, "__len__"):
            if len(samples) != 1:
                samples = [samples]
        elif hasattr(samples, "shape"):
            if len(samples.shape) > 0 and samples.shape[0] != 1:  # type: ignore
                samples = [samples]
        else:
            samples = [samples]

    if hasattr(samples, "__len__"):
        if index_length != len(samples):
            raise ValueError(
                f"Index length ({index_length}) and length of samples ({len(samples)}) must be equal for updating a tensor."
            )
    else:
        samples = [samples]

    return samples


def _warn_if_suboptimal_chunks(
    chunks_nbytes_after_updates: List[int], min_chunk_size: int, max_chunk_size: int
):
    upper_warn_threshold = max_chunk_size * (1 + CHUNK_UPDATE_WARN_PORTION)
    lower_warn_threshold = min_chunk_size * (1 - CHUNK_UPDATE_WARN_PORTION)

    for nbytes in chunks_nbytes_after_updates:
        if nbytes > upper_warn_threshold or nbytes < lower_warn_threshold:
            warnings.warn(
                "After update, some chunks were suboptimal. Be careful when doing lots of updates that modify the sizes of samples by a large amount, these can heavily impact read performance!"
            )
            break
