"""``DataCatalog`` stores instances of ``AbstractDataSet`` implementations to
provide ``load`` and ``save`` capabilities from anywhere in the program. To
use a ``DataCatalog``, you need to instantiate it with a dictionary of data
sets. Then it will act as a single point of reference for your calls,
relaying load and save functions to the underlying data sets.
"""
from __future__ import annotations

import copy
import difflib
import logging
import re
from collections import defaultdict
from typing import Any, Iterable

from parse import parse

from kedro.io.core import (
    AbstractDataSet,
    AbstractVersionedDataSet,
    DatasetAlreadyExistsError,
    DatasetError,
    DatasetNotFoundError,
    Version,
    generate_timestamp,
)
from kedro.io.memory_dataset import MemoryDataset

CATALOG_KEY = "catalog"
CREDENTIALS_KEY = "credentials"
WORDS_REGEX_PATTERN = re.compile(r"\W+")


def _get_credentials(
    credentials_name: str, credentials: dict[str, Any]
) -> dict[str, Any]:
    """Return a set of credentials from the provided credentials dict.

    Args:
        credentials_name: Credentials name.
        credentials: A dictionary with all credentials.

    Returns:
        The set of requested credentials.

    Raises:
        KeyError: When a data set with the given name has not yet been
            registered.

    """
    try:
        return credentials[credentials_name]
    except KeyError as exc:
        raise KeyError(
            f"Unable to find credentials '{credentials_name}': check your data "
            "catalog and credentials configuration. See "
            "https://kedro.readthedocs.io/en/stable/kedro.io.DataCatalog.html "
            "for an example."
        ) from exc


def _resolve_credentials(
    config: dict[str, Any], credentials: dict[str, Any]
) -> dict[str, Any]:
    """Return the dataset configuration where credentials are resolved using
    credentials dictionary provided.

    Args:
        config: Original dataset config, which may contain unresolved credentials.
        credentials: A dictionary with all credentials.

    Returns:
        The dataset config, where all the credentials are successfully resolved.
    """
    config = copy.deepcopy(config)

    def _map_value(key: str, value: Any) -> Any:
        if key == CREDENTIALS_KEY and isinstance(value, str):
            return _get_credentials(value, credentials)
        if isinstance(value, dict):
            return {k: _map_value(k, v) for k, v in value.items()}
        return value

    return {k: _map_value(k, v) for k, v in config.items()}


def _sub_nonword_chars(data_set_name: str) -> str:
    """Replace non-word characters in data set names since Kedro 0.16.2.

    Args:
        data_set_name: The data set name registered in the data catalog.

    Returns:
        The name used in `DataCatalog.datasets`.
    """
    return re.sub(WORDS_REGEX_PATTERN, "__", data_set_name)


def _specificity(pattern: str) -> int:
    """Helper function to check the length of exactly matched characters not inside brackets
    Example -
    specificity("{namespace}.companies") = 10
    specificity("{namespace}.{dataset}") = 1
    specificity("france.companies") = 16
    Args:
        pattern: The factory pattern
    """
    # Remove all the placeholders from the pattern
    result = re.sub(r"\{.*?\}", "", pattern)
    return len(result)


class _FrozenDatasets:
    """Helper class to access underlying loaded datasets."""

    def __init__(
        self,
        *datasets_collections: _FrozenDatasets | dict[str, AbstractDataSet],
    ):
        """Return a _FrozenDatasets instance from some datasets collections.
        Each collection could either be another _FrozenDatasets or a dictionary.
        """
        for collection in datasets_collections:
            if isinstance(collection, _FrozenDatasets):
                self.__dict__.update(collection.__dict__)
            else:
                # Non-word characters in dataset names are replaced with `__`
                # for easy access to transcoded/prefixed datasets.
                self.__dict__.update(
                    {
                        _sub_nonword_chars(dataset_name): dataset
                        for dataset_name, dataset in collection.items()
                    }
                )

    # Don't allow users to add/change attributes on the fly
    def __setattr__(self, key, value):
        msg = "Operation not allowed! "
        if key in self.__dict__:
            msg += "Please change datasets through configuration."
        else:
            msg += "Please use DataCatalog.add() instead."
        raise AttributeError(msg)


class DataCatalog:
    """``DataCatalog`` stores instances of ``AbstractDataSet`` implementations
    to provide ``load`` and ``save`` capabilities from anywhere in the
    program. To use a ``DataCatalog``, you need to instantiate it with
    a dictionary of data sets. Then it will act as a single point of reference
    for your calls, relaying load and save functions
    to the underlying data sets.
    """

    def __init__(
        self,
        data_sets: dict[str, AbstractDataSet] = None,
        feed_dict: dict[str, Any] = None,
        layers: dict[str, set[str]] = None,
        dataset_patterns: dict[str, dict[str, Any]] = None,
    ) -> None:
        """``DataCatalog`` stores instances of ``AbstractDataSet``
        implementations to provide ``load`` and ``save`` capabilities from
        anywhere in the program. To use a ``DataCatalog``, you need to
        instantiate it with a dictionary of data sets. Then it will act as a
        single point of reference for your calls, relaying load and save
        functions to the underlying data sets.

        Args:
            data_sets: A dictionary of data set names and data set instances.
            feed_dict: A feed dict with data to be added in memory.
            layers: A dictionary of data set layers. It maps a layer name
                to a set of data set names, according to the
                data engineering convention. For more details, see
                https://docs.kedro.org/en/stable/resources/glossary.html#layers-data-engineering-convention

        Example:
        ::

            >>> from kedro.extras.datasets.pandas import CSVDataSet
            >>>
            >>> cars = CSVDataSet(filepath="cars.csv",
            >>>                   load_args=None,
            >>>                   save_args={"index": False})
            >>> io = DataCatalog(data_sets={'cars': cars})
        """
        self._data_sets = dict(data_sets or {})
        self.datasets = _FrozenDatasets(self._data_sets)
        self.layers = layers
        # Keep a record of all patterns in the catalog.
        # {dataset pattern name : dataset pattern body}
        self.dataset_patterns = dict(dataset_patterns or {})
        # Sort all the patterns according to the parsing rules -
        # 1. Decreasing specificity (no of characters outside the brackets)
        # 2. Decreasing number of placeholders (no of curly brackets)
        # 3. Alphabetical
        self._sorted_dataset_patterns = sorted(
            self.dataset_patterns.keys(),
            key=lambda pattern: (
                -(_specificity(pattern)),
                -pattern.count("{"),
                pattern,
            ),
        )
        # Cache that stores {name : matched_pattern}
        self._pattern_matches_cache: dict[str, str] = {}
        # import the feed dict
        if feed_dict:
            self.add_feed_dict(feed_dict)

    @property
    def _logger(self):
        return logging.getLogger(__name__)

    @classmethod
    def from_config(
        cls: type,
        catalog: dict[str, dict[str, Any]] | None,
        credentials: dict[str, dict[str, Any]] = None,
        load_versions: dict[str, str] = None,
        save_version: str = None,
    ) -> DataCatalog:
        """Create a ``DataCatalog`` instance from configuration. This is a
        factory method used to provide developers with a way to instantiate
        ``DataCatalog`` with configuration parsed from configuration files.

        Args:
            catalog: A dictionary whose keys are the data set names and
                the values are dictionaries with the constructor arguments
                for classes implementing ``AbstractDataSet``. The data set
                class to be loaded is specified with the key ``type`` and their
                fully qualified class name. All ``kedro.io`` data set can be
                specified by their class name only, i.e. their module name
                can be omitted.
            credentials: A dictionary containing credentials for different
                data sets. Use the ``credentials`` key in a ``AbstractDataSet``
                to refer to the appropriate credentials as shown in the example
                below.
            load_versions: A mapping between dataset names and versions
                to load. Has no effect on data sets without enabled versioning.
            save_version: Version string to be used for ``save`` operations
                by all data sets with enabled versioning. It must: a) be a
                case-insensitive string that conforms with operating system
                filename limitations, b) always return the latest version when
                sorted in lexicographical order.

        Returns:
            An instantiated ``DataCatalog`` containing all specified
            data sets, created and ready to use.

        Raises:
            DatasetError: When the method fails to create any of the data
                sets from their config.
            DatasetNotFoundError: When `load_versions` refers to a dataset that doesn't
                exist in the catalog.

        Example:
        ::

            >>> config = {
            >>>     "cars": {
            >>>         "type": "pandas.CSVDataSet",
            >>>         "filepath": "cars.csv",
            >>>         "save_args": {
            >>>             "index": False
            >>>         }
            >>>     },
            >>>     "boats": {
            >>>         "type": "pandas.CSVDataSet",
            >>>         "filepath": "s3://aws-bucket-name/boats.csv",
            >>>         "credentials": "boats_credentials",
            >>>         "save_args": {
            >>>             "index": False
            >>>         }
            >>>     }
            >>> }
            >>>
            >>> credentials = {
            >>>     "boats_credentials": {
            >>>         "client_kwargs": {
            >>>             "aws_access_key_id": "<your key id>",
            >>>             "aws_secret_access_key": "<your secret>"
            >>>         }
            >>>      }
            >>> }
            >>>
            >>> catalog = DataCatalog.from_config(config, credentials)
            >>>
            >>> df = catalog.load("cars")
            >>> catalog.save("boats", df)
        """
        data_sets = {}
        dataset_patterns = {}
        catalog = copy.deepcopy(catalog) or {}
        credentials = copy.deepcopy(credentials) or {}
        save_version = save_version or generate_timestamp()
        load_versions = copy.deepcopy(load_versions) or {}

        missing_keys = load_versions.keys() - catalog.keys()
        if missing_keys:
            raise DatasetNotFoundError(
                f"'load_versions' keys [{', '.join(sorted(missing_keys))}] "
                f"are not found in the catalog."
            )

        layers: dict[str, set[str]] = defaultdict(set)
        for ds_name, ds_config in catalog.items():
            # Assume that any name with "}" in it is a dataset factory to be matched.
            if "}" in ds_name:
                # Add each factory to the dataset_patterns dict.
                dataset_patterns[ds_name] = ds_config
            else:
                ds_layer = ds_config.pop("layer", None)
                if ds_layer is not None:
                    layers[ds_layer].add(ds_name)

                ds_config = _resolve_credentials(ds_config, credentials)
                data_sets[ds_name] = AbstractDataSet.from_config(
                    ds_name, ds_config, load_versions.get(ds_name), save_version
                )
        dataset_layers = layers or None
        return cls(
            data_sets=data_sets,
            layers=dataset_layers,
            dataset_patterns=dataset_patterns,
        )

    def _get_dataset(
        self, data_set_name: str, version: Version = None, suggest: bool = True
    ) -> AbstractDataSet:
        if data_set_name not in self._data_sets:
            # When a dataset is "used" in the pipeline that's not in the recorded catalog datasets,
            # try to match it against the data factories in the catalog. If it's a match,
            # resolve it to a dataset instance and add it to the catalog, so it only needs
            # to be matched once and not everytime the dataset is used in the pipeline.
            if self.exists_in_catalog_config(data_set_name):
                pattern = self._pattern_matches_cache[data_set_name]
                matched_dataset = self._resolve_dataset(data_set_name, pattern)
                self.add(data_set_name, matched_dataset)
            else:
                error_msg = f"DataSet '{data_set_name}' not found in the catalog"

                # Flag to turn on/off fuzzy-matching which can be time consuming and
                # slow down plugins like `kedro-viz`
                if suggest:
                    matches = difflib.get_close_matches(
                        data_set_name, self._data_sets.keys()
                    )
                    if matches:
                        suggestions = ", ".join(matches)
                        error_msg += (
                            f" - did you mean one of these instead: {suggestions}"
                        )

                raise DatasetNotFoundError(error_msg)

        data_set = self._data_sets[data_set_name]
        if version and isinstance(data_set, AbstractVersionedDataSet):
            # we only want to return a similar-looking dataset,
            # not modify the one stored in the current catalog
            data_set = data_set._copy(  # pylint: disable=protected-access
                _version=version
            )

        return data_set

    def _resolve_dataset(
        self, dataset_name: str, matched_pattern: str
    ) -> AbstractDataSet:
        """Get resolved AbstractDataSet from a factory config"""
        result = parse(matched_pattern, dataset_name)
        template_copy = copy.deepcopy(self.dataset_patterns[matched_pattern])
        # Resolve the factory config for the dataset
        for key, value in template_copy.items():
            if isinstance(value, Iterable) and "}" in value:
                string_value = str(value)
                # result.named: gives access to all dict items in the match result.
                # format_map fills in dict values into a string with {...} placeholders
                # of the same key name.
                try:
                    template_copy[key] = string_value.format_map(result.named)
                except KeyError as exc:
                    raise DatasetError(
                        f"Unable to resolve '{key}' for the pattern '{matched_pattern}'"
                    ) from exc
        # Create dataset from catalog template.
        return AbstractDataSet.from_config(dataset_name, template_copy)

    def load(self, name: str, version: str = None) -> Any:
        """Loads a registered data set.

        Args:
            name: A data set to be loaded.
            version: Optional argument for concrete data version to be loaded.
                Works only with versioned datasets.

        Returns:
            The loaded data as configured.

        Raises:
            DatasetNotFoundError: When a data set with the given name
                has not yet been registered.

        Example:
        ::

            >>> from kedro.io import DataCatalog
            >>> from kedro.extras.datasets.pandas import CSVDataSet
            >>>
            >>> cars = CSVDataSet(filepath="cars.csv",
            >>>                   load_args=None,
            >>>                   save_args={"index": False})
            >>> io = DataCatalog(data_sets={'cars': cars})
            >>>
            >>> df = io.load("cars")
        """
        load_version = Version(version, None) if version else None
        dataset = self._get_dataset(name, version=load_version)

        self._logger.info(
            "Loading data from '%s' (%s)...", name, type(dataset).__name__
        )

        result = dataset.load()

        return result

    def save(self, name: str, data: Any) -> None:
        """Save data to a registered data set.

        Args:
            name: A data set to be saved to.
            data: A data object to be saved as configured in the registered
                data set.

        Raises:
            DatasetNotFoundError: When a data set with the given name
                has not yet been registered.

        Example:
        ::

            >>> import pandas as pd
            >>>
            >>> from kedro.extras.datasets.pandas import CSVDataSet
            >>>
            >>> cars = CSVDataSet(filepath="cars.csv",
            >>>                   load_args=None,
            >>>                   save_args={"index": False})
            >>> io = DataCatalog(data_sets={'cars': cars})
            >>>
            >>> df = pd.DataFrame({'col1': [1, 2],
            >>>                    'col2': [4, 5],
            >>>                    'col3': [5, 6]})
            >>> io.save("cars", df)
        """
        dataset = self._get_dataset(name)

        self._logger.info("Saving data to '%s' (%s)...", name, type(dataset).__name__)

        dataset.save(data)

    def exists(self, name: str) -> bool:
        """Checks whether registered data set exists by calling its `exists()`
        method. Raises a warning and returns False if `exists()` is not
        implemented.

        Args:
            name: A data set to be checked.

        Returns:
            Whether the data set output exists.

        """
        try:
            dataset = self._get_dataset(name)
        except DatasetNotFoundError:
            return False
        return dataset.exists()

    def release(self, name: str):
        """Release any cached data associated with a data set

        Args:
            name: A data set to be checked.

        Raises:
            DatasetNotFoundError: When a data set with the given name
                has not yet been registered.
        """
        dataset = self._get_dataset(name)
        dataset.release()

    def add(
        self, data_set_name: str, data_set: AbstractDataSet, replace: bool = False
    ) -> None:
        """Adds a new ``AbstractDataSet`` object to the ``DataCatalog``.

        Args:
            data_set_name: A unique data set name which has not been
                registered yet.
            data_set: A data set object to be associated with the given data
                set name.
            replace: Specifies whether to replace an existing dataset
                with the same name is allowed.

        Raises:
            DatasetAlreadyExistsError: When a data set with the same name
                has already been registered.

        Example:
        ::

            >>> from kedro.extras.datasets.pandas import CSVDataSet
            >>>
            >>> io = DataCatalog(data_sets={
            >>>                   'cars': CSVDataSet(filepath="cars.csv")
            >>>                  })
            >>>
            >>> io.add("boats", CSVDataSet(filepath="boats.csv"))
        """
        if data_set_name in self._data_sets:
            if replace:
                self._logger.warning("Replacing dataset '%s'", data_set_name)
            else:
                raise DatasetAlreadyExistsError(
                    f"Dataset '{data_set_name}' has already been registered"
                )
        self._data_sets[data_set_name] = data_set
        self.datasets = _FrozenDatasets(self.datasets, {data_set_name: data_set})

    def add_all(
        self, data_sets: dict[str, AbstractDataSet], replace: bool = False
    ) -> None:
        """Adds a group of new data sets to the ``DataCatalog``.

        Args:
            data_sets: A dictionary of dataset names and dataset
                instances.
            replace: Specifies whether to replace an existing dataset
                with the same name is allowed.

        Raises:
            DatasetAlreadyExistsError: When a data set with the same name
                has already been registered.

        Example:
        ::

            >>> from kedro.extras.datasets.pandas import CSVDataSet, ParquetDataSet
            >>>
            >>> io = DataCatalog(data_sets={
            >>>                   "cars": CSVDataSet(filepath="cars.csv")
            >>>                  })
            >>> additional = {
            >>>     "planes": ParquetDataSet("planes.parq"),
            >>>     "boats": CSVDataSet(filepath="boats.csv")
            >>> }
            >>>
            >>> io.add_all(additional)
            >>>
            >>> assert io.list() == ["cars", "planes", "boats"]
        """
        for name, data_set in data_sets.items():
            self.add(name, data_set, replace)

    def add_feed_dict(self, feed_dict: dict[str, Any], replace: bool = False) -> None:
        """Adds instances of ``MemoryDataset``, containing the data provided
        through feed_dict.

        Args:
            feed_dict: A feed dict with data to be added in memory.
            replace: Specifies whether to replace an existing dataset
                with the same name is allowed.

        Example:
        ::

            >>> import pandas as pd
            >>>
            >>> df = pd.DataFrame({'col1': [1, 2],
            >>>                    'col2': [4, 5],
            >>>                    'col3': [5, 6]})
            >>>
            >>> io = DataCatalog()
            >>> io.add_feed_dict({
            >>>     'data': df
            >>> }, replace=True)
            >>>
            >>> assert io.load("data").equals(df)
        """
        for data_set_name in feed_dict:
            if isinstance(feed_dict[data_set_name], AbstractDataSet):
                data_set = feed_dict[data_set_name]
            else:
                data_set = MemoryDataset(data=feed_dict[data_set_name])

            self.add(data_set_name, data_set, replace)

    def list(self, regex_search: str | None = None) -> list[str]:
        """
        List of all dataset names registered in the catalog.
        This can be filtered by providing an optional regular expression
        which will only return matching keys.

        Args:
            regex_search: An optional regular expression which can be provided
                to limit the data sets returned by a particular pattern.
        Returns:
            A list of dataset names available which match the
            `regex_search` criteria (if provided). All data set names are returned
            by default.

        Raises:
            SyntaxError: When an invalid regex filter is provided.

        Example:
        ::

            >>> io = DataCatalog()
            >>> # get data sets where the substring 'raw' is present
            >>> raw_data = io.list(regex_search='raw')
            >>> # get data sets which start with 'prm' or 'feat'
            >>> feat_eng_data = io.list(regex_search='^(prm|feat)')
            >>> # get data sets which end with 'time_series'
            >>> models = io.list(regex_search='.+time_series$')
        """

        if regex_search is None:
            return list(self._data_sets.keys())

        if not regex_search.strip():
            self._logger.warning("The empty string will not match any data sets")
            return []

        try:
            pattern = re.compile(regex_search, flags=re.IGNORECASE)

        except re.error as exc:
            raise SyntaxError(
                f"Invalid regular expression provided: '{regex_search}'"
            ) from exc
        return [dset_name for dset_name in self._data_sets if pattern.search(dset_name)]

    def exists_in_catalog_config(self, dataset_name: str) -> bool:
        """Check if a dataset exists in the catalog as an exact match or if it matches a pattern."""
        if (
            dataset_name in self._data_sets
            or dataset_name in self._pattern_matches_cache
        ):
            return True
        matched_pattern = self.match_name_against_patterns(dataset_name)
        if matched_pattern:
            # cache the "dataset_name -> pattern" match
            self._pattern_matches_cache[dataset_name] = matched_pattern
            return True
        return False

    def match_name_against_patterns(self, dataset_name: str) -> str | None:
        """Match a dataset name against existing patterns"""
        # Loop through all dataset patterns and check if the given dataset name has a match.
        for pattern in self._sorted_dataset_patterns:
            result = parse(pattern, dataset_name)
            if result:
                return pattern
        return None

    def shallow_copy(self) -> DataCatalog:
        """Returns a shallow copy of the current object.

        Returns:
            Copy of the current object.
        """
        return DataCatalog(
            data_sets=self._data_sets,
            layers=self.layers,
            dataset_patterns=self.dataset_patterns,
        )

    def __eq__(self, other):
        return (self._data_sets, self.layers, self.dataset_patterns) == (
            other._data_sets,
            other.layers,
            other.dataset_patterns,
        )

    def confirm(self, name: str) -> None:
        """Confirm a dataset by its name.

        Args:
            name: Name of the dataset.
        Raises:
            DatasetError: When the dataset does not have `confirm` method.

        """
        self._logger.info("Confirming dataset '%s'", name)
        data_set = self._get_dataset(name)

        if hasattr(data_set, "confirm"):
            data_set.confirm()  # type: ignore
        else:
            raise DatasetError(f"Dataset '{name}' does not have 'confirm' method")
