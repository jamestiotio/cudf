# Copyright (c) 2018, NVIDIA CORPORATION.

from numbers import Number
import collections
import itertools

import pandas as pd

import cudf
from cudf.dataframe.dataframe import DataFrame
from cudf.dataframe.series import Series
from cudf import MultiIndex
from cudf.bindings.nvtx import nvtx_range_pop

from cudf.bindings.groupby import apply_groupby as cpp_apply_groupby


def columns_from_dataframe(df):
    cols = [sr._column for sr in df._cols.values()]
    return cols


def dataframe_from_columns(cols, index_cols=None, index=None, columns=None):
    df = cudf.DataFrame(dict(zip(range(len(cols)), cols)), index=index)
    if columns is not None:
        df.columns = columns
    return df


class _Groupby(object):
    def sum(self):
        return self._apply_aggregation("sum")

    def min(self):
        return self._apply_aggregation("min")

    def max(self):
        return self._apply_aggregation("max")

    def mean(self):
        return self._apply_aggregation("mean")

    def count(self):
        return self._apply_aggregation("count")

    def agg(self, func):
        return self._apply_aggregation(func)

class SeriesGroupBy(_Groupby):

    def __init__(self, sr, by, method="hash", level=None, sort=True):
        self._sr = sr
        self._by = by
        self._sort = sort
        self._groupby = _GroupbyHelper(sr, by, level=level, sort=sort)

    def _apply_aggregation(self, agg):
        return self._groupby.compute_result(agg)

class DataFrameGroupBy(_Groupby):

    def __init__(self, df, by, method="hash", as_index=True, level=None, sort=True):
        """
        Parameters
        ----------
        df : DataFrame
        by : str, list
            - str
                The column name to group on.
            - list
                List of *str* of the column names to group on.
        method : str, optional
            A string indicating the libgdf method to use to perform the
            group by. Valid values are "hash".
        """
        self._df = df
        self._by = by
        self._as_index = as_index
        self._sort = sort
        self._groupby = _GroupbyHelper(df, by, as_index=as_index, level=level, sort=sort)

    def _apply_aggregation(self, agg):
        """
        Applies the aggregation function(s) ``agg`` on all columns
        """
        return self._groupby.compute_result(agg)

    def __getitem__(self, arg):
        if isinstance(arg, str):
            return self.__getattr__(arg)
        else:
            arg = list(arg)
            by_list = []
            for by_name, by in zip(self._groupby.key_names, self._groupby.key_columns):
                by_list.append(cudf.Series(by, name=by_name))
            return self._df[arg].groupby(by_list,
                                         sort=self._sort)

    def __getattr__(self, key):
        if key not in self._df.columns:
            raise AttributeError("'DataFrameGroupBy' object has no attribute "
                                 "'{}'".format(key))
        by_list = []
        for by_name, by in zip(self._groupby.key_names, self._groupby.key_columns):
            by_list.append(cudf.Series(by, name=by_name))
        return self._df[key].groupby(by_list,
                                     sort=self._sort)


class _GroupbyHelper(object):

    NAMED_AGGS = ('sum', 'mean', 'min', 'max', 'count')

    def __init__(self, obj, by, as_index=True, level=None, sort=None):
        """
        Helper class for both SeriesGroupBy and DataFrameGroupBy classes.
        """
        self.obj = obj
        self.by = by
        self.as_index = as_index
        self.level = level
        self.sort = sort
        self.normalize_keys()

    def normalize_keys(self):
        """
        Sets self.key_names and self.key_columns
        """
        if isinstance(self.by, (list, tuple)):
            self.key_names = []
            self.key_columns = []
            for by in self.by:
                name, col = self.key_from_by(by)
                self.key_names.append(name)
                self.key_columns.append(col)
        else:
            # grouping by a single label or Series
            name, col = self.key_from_by(self.by)
            self.key_names = [name]
            self.key_columns = [col]

    def key_from_by(self, by):
        """
        Get (key_name, key_column) pair from a single *by* argument
        """
        if isinstance(by, str):
            key_name = by
            key_column = self.obj[by]._column
        else:
            try:
                by = cudf.Series(by)
            except:
                raise ValueError("Cannot convert by argument to a Series")
            if len(by) != len(self.obj):
                raise NotImplementedError("cuDF does not support arbitrary series index lengths "
                                        "for groupby")
            key_name = by.name
            key_column = by._column
        return key_name, key_column

    def compute_result(self, agg):
        """
        Computes the groupby result
        """
        self.normalize_agg(agg)
        self.normalize_values()
        aggs_as_list = self.get_aggs_as_list()

        out_key_columns, out_value_columns = _groupby_engine(
            self.key_columns,
            self.value_columns,
            aggs_as_list,
            self.sort
        )

        return self.construct_result(out_key_columns, out_value_columns)

    def normalize_agg(self, agg):
        """
        Normalize agg to a dictionary with column names
        as keys and lists of aggregations as values.

        For a Series, the dictionary has a single key ``None``
        """
        if isinstance(agg, collections.Mapping):
            for key, val in agg.items():
                if not isinstance(val, list):
                    agg[key] = [val]
            self.aggs = agg
            return
        if isinstance(self.obj, cudf.Series):
            keys = [None]
        else:
            keys = []
            for col_name in self.obj.columns:
                if col_name not in self.key_names:
                    keys.append(col_name)
        if isinstance(agg, str):
            agg = [agg]
        values = [agg] * len(keys)
        self.aggs = dict(zip(keys, values))
        self.validate_aggs()

    def validate_aggs(self):
        for col_name, agg_list in self.aggs.items():
            for agg in agg_list:
                if agg not in self.NAMED_AGGS:
                    raise ValueError(
                        f"Aggregation function name {agg} not recognized"
                    )

    def normalize_values(self):
        """
        Sets self.value_names and self.value_columns
        """
        if isinstance(self.obj, cudf.Series):
            # SeriesGroupBy
            col = self.obj._column
            agg_list = self.aggs[None]
            if len(agg_list) == 1:
                self.value_columns = [col]
                self.value_names = [self.obj.name]
            else:
                self.value_columns = [col] * len(agg_list)
                self.value_names = agg_list
        else:
            # DataFraneGroupBy
            self.value_columns = []
            self.value_names = []
            for col_name, agg_list in self.aggs.items():
                col = self.obj[col_name]._column
                if len(agg_list) == 1:
                    agg_name = agg_list[0]
                    self.value_columns.append(col)
                    self.value_names.append(col_name)
                else:
                    self.value_columns.extend([col]*len(agg_list))
                    self.value_names.extend([col_name]*len(agg_list))

    def construct_result(self, out_key_columns, out_value_columns):
        if not self.as_index:
            result = cudf.concat(
                [
                    dataframe_from_columns(out_key_columns, columns=self.key_names),
                    dataframe_from_columns(out_value_columns, columns=self.value_names)
                ],
                axis=1
            )
            return result

        result = dataframe_from_columns(
                out_value_columns,
                columns=self.compute_result_column_index()
            )
        result.index = self.compute_result_index(out_key_columns, out_value_columns)

        if isinstance(self.obj, cudf.Series):
            # May need to downcast from DataFrame to Series:
            if len(self.aggs[None]) == 1:
                result = result[result.columns[0]]
                result.name = self.value_names[0]

        return result


    def compute_result_index(self, key_columns, value_columns):
        """
        Computes the index of the result
        """
        key_names = self.key_names
        if (len(key_columns)) == 1:
            return cudf.dataframe.index.as_index(key_columns[0],
                                                 name=key_names[0])
        else:
            empty_keys = all([len(x)==0 for x in key_columns])
            if len(value_columns) == 0  and empty_keys:
                return cudf.dataframe.index.GenericIndex(cudf.Series([], dtype='object'))
            return MultiIndex(source_data=dataframe_from_columns(key_columns,
                                                                 columns=key_names))

    def compute_result_column_index(self):
        """
        Computes the column index of the result
        """
        value_names = self.value_names
        aggs_as_list = self.get_aggs_as_list()

        if isinstance(self.obj, cudf.Series):
            if len(aggs_as_list) == 1:
                return self.obj.name
            else:
                return aggs_as_list
        else:
            if len(aggs_as_list) == len(self.aggs):
                return value_names
            else:
                return MultiIndex.from_tuples(zip(value_names, aggs_as_list))

    def get_aggs_as_list(self):
        """
        Returns self.aggs as a list of aggs
        """
        aggs_as_list = list(itertools.chain.from_iterable(self.aggs.values()))
        return aggs_as_list


def _groupby_engine(key_columns, value_columns, aggs, sort):
    """
    Parameters
    ----------
    key_columns : list of Columns
    value_columns : list of Columns
    aggs : list of str
    sort : bool

    Returns
    -------
    out_key_columns : list of Columns
    out_value_columns : list of Columns
    """
    out_key_columns, out_value_columns = cpp_apply_groupby(
        key_columns,
        value_columns,
        aggs
    )

    if sort:
        key_names = ["key_"+str(i) for i in range(len(key_columns))]
        value_names = ["value_"+str(i) for i in range(len(value_columns))]
        value_names = _add_prefixes(value_names, aggs)

        # concatenate
        result = cudf.concat(
            [
                dataframe_from_columns(out_key_columns, columns=key_names),
                dataframe_from_columns(out_value_columns, columns=value_names)
            ],
            axis=1
        )

        # sort values
        result = result.sort_values(key_names)

        # split
        out_key_columns = columns_from_dataframe(result[key_names])
        out_value_columns = columns_from_dataframe(result[value_names])

    return out_key_columns, out_value_columns

def _add_prefixes(names, prefixes):
    """
    Return a copy of ``names`` prefixed with ``prefixes``
    """
    prefixed_names = names.copy()
    if isinstance(prefixes, str):
        prefix = prefixes
        for i, col_name in enumerate(names):
            prefixed_names[i] = f"{prefix}_{col_name}"
    else:
        for i, (prefix, col_name) in enumerate(zip(prefixes, names)):
            prefixed_names[i] = f"{prefix}_{col_name}"
    return prefixed_names
