import re
from functools import partial
from warnings import warn
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Type, TypeVar, Union
from typing import Collection, Sequence, Mapping

import h5py
import numpy as np
import pandas as pd
from scipy import sparse

from .._core.sparse_dataset import SparseDataset
from .._core.file_backing import AnnDataFileManager
from .._core.anndata import AnnData
from ..compat import (
    _from_fixed_length_strings,
    _decode_structured_array,
    _clean_uns,
    Literal,
)
from .utils import (
    H5PY_V3,
    report_read_key_on_error,
    report_write_key_on_error,
    idx_chunks_along_axis,
    _read_legacy_raw,
)
from .specs import read_elem, write_elem
from anndata._warnings import OldFormatWarning

T = TypeVar("T")


def write_h5ad(
    filepath: Union[Path, str],
    adata: AnnData,
    *,
    force_dense: bool = None,
    as_dense: Sequence[str] = (),
    dataset_kwargs: Mapping = MappingProxyType({}),
    **kwargs,
) -> None:
    if force_dense is not None:
        warn(
            "The `force_dense` argument is deprecated. Use `as_dense` instead.",
            FutureWarning,
        )
    if force_dense is True:
        if adata.raw is not None:
            as_dense = ("X", "raw/X")
        else:
            as_dense = ("X",)
    if isinstance(as_dense, str):
        as_dense = [as_dense]
    if "raw.X" in as_dense:
        as_dense = list(as_dense)
        as_dense[as_dense.index("raw.X")] = "raw/X"
    if any(val not in {"X", "raw/X"} for val in as_dense):
        raise NotImplementedError(
            "Currently, only `X` and `raw/X` are supported values in `as_dense`"
        )
    if "raw/X" in as_dense and adata.raw is None:
        raise ValueError("Cannot specify writing `raw/X` to dense if it doesn’t exist.")

    adata.strings_to_categoricals()
    if adata.raw is not None:
        adata.strings_to_categoricals(adata.raw.var)
    dataset_kwargs = {**dataset_kwargs, **kwargs}
    filepath = Path(filepath)
    mode = "a" if adata.isbacked else "w"
    if adata.isbacked:  # close so that we can reopen below
        adata.file.close()
    with h5py.File(filepath, mode) as f:
        # TODO: Use spec writing system for this
        f = f["/"]
        f.attrs.setdefault("encoding-type", "anndata")
        f.attrs.setdefault("encoding-version", "0.1.0")

        if not adata.isbacked or adata._has_X():
            if "X" in as_dense and isinstance(adata.X, (sparse.spmatrix, SparseDataset)):
                write_sparse_as_dense(f, "X", adata.X, dataset_kwargs=dataset_kwargs)
            elif not (adata.isbacked and Path(adata.filename) == Path(filepath)) or adata.is_view:
                # If adata.isbacked, X should already be up to date
                write_elem(f, "X", adata.X, dataset_kwargs=dataset_kwargs)
        if "raw/X" in as_dense and isinstance(
            adata.raw.X, (sparse.spmatrix, SparseDataset)
        ):
            write_sparse_as_dense(
                f, "raw/X", adata.raw.X, dataset_kwargs=dataset_kwargs
            )
            write_elem(f, "raw/var", adata.raw.var, dataset_kwargs=dataset_kwargs)
            write_elem(
                f, "raw/varm", dict(adata.raw.varm), dataset_kwargs=dataset_kwargs
            )
        elif adata.raw is not None:
            write_elem(f, "raw", adata.raw, dataset_kwargs=dataset_kwargs)
        write_elem(f, "obs", adata.obs, dataset_kwargs=dataset_kwargs)
        write_elem(f, "var", adata.var, dataset_kwargs=dataset_kwargs)
        write_elem(f, "obsm", dict(adata.obsm), dataset_kwargs=dataset_kwargs)
        write_elem(f, "varm", dict(adata.varm), dataset_kwargs=dataset_kwargs)
        write_elem(f, "obsp", dict(adata.obsp), dataset_kwargs=dataset_kwargs)
        write_elem(f, "varp", dict(adata.varp), dataset_kwargs=dataset_kwargs)
        write_elem(f, "layers", dict(adata.layers), dataset_kwargs=dataset_kwargs)
        write_elem(f, "uns", dict(adata.uns), dataset_kwargs=dataset_kwargs)


@report_write_key_on_error
def write_sparse_as_dense(f, key, value, dataset_kwargs=MappingProxyType({})):
    real_key = None  # Flag for if temporary key was used
    if key in f:
        if (
            isinstance(value, (h5py.Group, h5py.Dataset, SparseDataset))
            and value.file.filename == f.file.filename
        ):  # Write to temporary key before overwriting
            real_key = key
            # Transform key to temporary, e.g. raw/X -> raw/_X, or X -> _X
            key = re.sub(r"(.*)(\w(?!.*/))", r"\1_\2", key.rstrip("/"))
        else:
            del f[key]  # Wipe before write
    dset = f.create_dataset(key, shape=value.shape, dtype=value.dtype, **dataset_kwargs)
    compressed_axis = int(isinstance(value, sparse.csc_matrix))
    for idx in idx_chunks_along_axis(value.shape, compressed_axis, 1000):
        dset[idx] = value[idx].toarray()
    if real_key is not None:
        del f[real_key]
        f[real_key] = f[key]
        del f[key]


def read_h5ad_backed(filename: Union[str, Path], mode: Literal["r", "r+"]) -> AnnData:
    d = dict(filename=filename, filemode=mode)

    with h5py.File(filename, mode) as f:
        attributes = ["obsm", "varm", "obsp", "varp", "uns", "layers"]
        df_attributes = ["obs", "var"]

        if "encoding-type" in f.attrs:
            attributes.extend(df_attributes)
        else:
            for k in df_attributes:
                if k in f:  # Backwards compat
                    d[k] = read_dataframe(f[k])

        d.update({k: read_elem(f[k]) for k in attributes if k in f})

        d["raw"] = _read_raw(f, attrs={"var", "varm"})

        X_dset = f.get("X", None)
        if X_dset is None:
            pass
        elif isinstance(X_dset, h5py.Group):
            d["dtype"] = X_dset["data"].dtype
        elif hasattr(X_dset, "dtype"):
            d["dtype"] = f["X"].dtype
        else:
            raise ValueError()

        _clean_uns(d)

        return AnnData(**d)


def read_h5ad(
    filename: Union[str, Path],
    backed: Union[Literal["r", "r+"], bool, None] = None,
    *,
    as_sparse: Sequence[str] = (),
    as_sparse_fmt: Type[sparse.spmatrix] = sparse.csr_matrix,
    chunk_size: int = 6000,  # TODO, probably make this 2d chunks
) -> AnnData:
    """\
    Read `.h5ad`-formatted hdf5 file.

    Parameters
    ----------
    filename
        File name of data file.
    backed
        If `'r'`, load :class:`~anndata.AnnData` in `backed` mode
        instead of fully loading it into memory (`memory` mode).
        If you want to modify backed attributes of the AnnData object,
        you need to choose `'r+'`.
    as_sparse
        If an array was saved as dense, passing its name here will read it as
        a sparse_matrix, by chunk of size `chunk_size`.
    as_sparse_fmt
        Sparse format class to read elements from `as_sparse` in as.
    chunk_size
        Used only when loading sparse dataset that is stored as dense.
        Loading iterates through chunks of the dataset of this row size
        until it reads the whole dataset.
        Higher size means higher memory consumption and higher (to a point)
        loading speed.
    """
    if backed not in {None, False}:
        mode = backed
        if mode is True:
            mode = "r+"
        assert mode in {"r", "r+"}
        return read_h5ad_backed(filename, mode)

    if as_sparse_fmt not in (sparse.csr_matrix, sparse.csc_matrix):
        raise NotImplementedError(
            "Dense formats can only be read to CSR or CSC matrices at this time."
        )
    if isinstance(as_sparse, str):
        as_sparse = [as_sparse]
    else:
        as_sparse = list(as_sparse)
    for i in range(len(as_sparse)):
        if as_sparse[i] in {("raw", "X"), "raw.X"}:
            as_sparse[i] = "raw/X"
        elif as_sparse[i] not in {"raw/X", "X"}:
            raise NotImplementedError(
                "Currently only `X` and `raw/X` can be read as sparse."
            )

    rdasp = partial(
        read_dense_as_sparse, sparse_format=as_sparse_fmt, axis_chunk=chunk_size
    )

    with h5py.File(filename, "r") as f:
        d = {}
        for k in f.keys():
            # Backwards compat for old raw
            if k == "raw" or k.startswith("raw."):
                continue
            if k == "X" and "X" in as_sparse:
                d[k] = rdasp(f[k])
            elif k == "raw":
                assert False, "unexpected raw format"
            elif k in {"obs", "var"}:
                # Backwards compat
                d[k] = read_dataframe(f[k])
            else:  # Base case
                d[k] = read_elem(f[k])

        d["raw"] = _read_raw(f, as_sparse, rdasp)

        X_dset = f.get("X", None)
        if X_dset is None:
            pass
        elif isinstance(X_dset, h5py.Group):
            d["dtype"] = X_dset["data"].dtype
        elif hasattr(X_dset, "dtype"):
            d["dtype"] = f["X"].dtype
        else:
            raise ValueError()

    _clean_uns(d)  # backwards compat

    return AnnData(**d)


def _read_raw(
    f: Union[h5py.File, AnnDataFileManager],
    as_sparse: Collection[str] = (),
    rdasp: Callable[[h5py.Dataset], sparse.spmatrix] = None,
    *,
    attrs: Collection[str] = ("X", "var", "varm"),
):
    if as_sparse:
        assert rdasp is not None, "must supply rdasp if as_sparse is supplied"
    raw = {}
    if "X" in attrs and "raw/X" in f:
        read_x = rdasp if "raw/X" in as_sparse else read_elem
        raw["X"] = read_x(f["raw/X"])
    for v in ("var", "varm"):
        if v in attrs and f"raw/{v}" in f:
            raw[v] = read_elem(f[f"raw/{v}"])
    return _read_legacy_raw(f, raw, read_dataframe, read_elem, attrs=attrs)


@report_read_key_on_error
def read_dataframe_legacy(dataset) -> pd.DataFrame:
    """Read pre-anndata 0.7 dataframes."""
    warn(
        f"'{dataset.name}' was written with a very old version of AnnData. "
        "Consider rewriting it.",
        OldFormatWarning,
    )
    if H5PY_V3:
        df = pd.DataFrame(
            _decode_structured_array(
                _from_fixed_length_strings(dataset[()]), dtype=dataset.dtype
            )
        )
    else:
        df = pd.DataFrame(_from_fixed_length_strings(dataset[()]))
    df.set_index(df.columns[0], inplace=True)
    return df


def read_dataframe(group) -> pd.DataFrame:
    """Backwards compat function"""
    if not isinstance(group, h5py.Group):
        return read_dataframe_legacy(group)
    else:
        return read_elem(group)


@report_read_key_on_error
def read_dataset(dataset: h5py.Dataset):
    if H5PY_V3:
        string_dtype = h5py.check_string_dtype(dataset.dtype)
        if (string_dtype is not None) and (string_dtype.encoding == "utf-8"):
            dataset = dataset.asstr()
    value = dataset[()]
    if not hasattr(value, "dtype"):
        return value
    elif isinstance(value.dtype, str):
        pass
    elif issubclass(value.dtype.type, np.string_):
        value = value.astype(str)
        # Backwards compat, old datasets have strings as one element 1d arrays
        if len(value) == 1:
            return value[0]
    elif len(value.dtype.descr) > 1:  # Compound dtype
        # For backwards compat, now strings are written as variable length
        dtype = value.dtype
        value = _from_fixed_length_strings(value)
        if H5PY_V3:
            value = _decode_structured_array(value, dtype=dtype)
    if value.shape == ():
        value = value[()]
    return value


@report_read_key_on_error
def read_dense_as_sparse(
    dataset: h5py.Dataset, sparse_format: sparse.spmatrix, axis_chunk: int
):
    if sparse_format == sparse.csr_matrix:
        return read_dense_as_csr(dataset, axis_chunk)
    elif sparse_format == sparse.csc_matrix:
        return read_dense_as_csc(dataset, axis_chunk)
    else:
        raise ValueError(f"Cannot read dense array as type: {sparse_format}")


def read_dense_as_csr(dataset, axis_chunk=6000):
    sub_matrices = []
    for idx in idx_chunks_along_axis(dataset.shape, 0, axis_chunk):
        dense_chunk = dataset[idx]
        sub_matrix = sparse.csr_matrix(dense_chunk)
        sub_matrices.append(sub_matrix)
    return sparse.vstack(sub_matrices, format="csr")


def read_dense_as_csc(dataset, axis_chunk=6000):
    sub_matrices = []
    for idx in idx_chunks_along_axis(dataset.shape, 1, axis_chunk):
        sub_matrix = sparse.csc_matrix(dataset[idx])
        sub_matrices.append(sub_matrix)
    return sparse.hstack(sub_matrices, format="csc")
