# Copyright 2021 National Technology & Engineering Solutions of Sandia, LLC (NTESS).
# Under the terms of Contract DE-NA0003525 with NTESS,
# the U.S. Government retains certain rights in this software.
"""This modules contains utilities for generating synthetic gamma spectrum templates from GADRAS."""
import os
from contextlib import contextmanager
from copy import deepcopy
from typing import Iterator, List, Tuple, Union

import numpy as np
import pandas as pd
import yaml
from numpy.random import Generator

from riid.data.sampleset import SampleSet, _get_utc_timestamp, read_pcf
from riid.gadras.api import (DETECTOR_PARAMS, GADRAS_ASSEMBLY_PATH,
                             INJECT_PARAMS, SourceInjector, get_gadras_api,
                             get_inject_setups, validate_inject_config)


class SeedSynthesizer():

    @contextmanager
    def _cwd(self, path):
        """Temporarily change working directory.

        This is used to change the execution location which necessary due to how the GADRAS API
        uses relative pathing from its installation directory.
        """
        oldpwd = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(oldpwd)

    def _get_detector_parameters(self, gadras_api) -> dict:
        params = {}
        for k in gadras_api.detectorGetParameters().Keys:
            if k not in DETECTOR_PARAMS:
                continue
            params[k] = gadras_api.detectorGetParameter(k)
        return params

    def _set_detector_parameters(self, gadras_api, new_parameters: dict, verbose=False,
                                 dry_run=False) -> None:
        for k, v in new_parameters.items():
            k_upper = k.upper()
            if k_upper in INJECT_PARAMS:
                continue
            v_type = DETECTOR_PARAMS[k_upper]["type"]

            if v_type == "float":
                gadras_api.detectorSetParameter(k_upper, float(v))
                if verbose:
                    print(f"i: Setting parameter '{k_upper}' to {v}")
            elif v_type == "int":
                gadras_api.detectorSetParameter(k_upper.upper(), int(v))
                if verbose:
                    print(f"i: Setting parameter '{k_upper}' to {v}")
            else:
                print(f"Warning: parameter '{k}'s type of {v_type} is not supported - not set.")
        if not dry_run:
            gadras_api.detectorSaveParameters()

    def generate(self, config: Union[str, dict],
                 normalize_spectra: bool = True, normalize_sources: bool = True,
                 verbose: bool = False) -> SampleSet:
        """Produce a `SampleSet` containing foreground and/or background seeds using GADRAS based
        on the given inject configuration.

        Args:
            config: dictionary is treated as the actual config containing the needed information
                to perform injects via the GADRAS API, while a string is treated as a path to a YAML
                file which deserialized as a dictionary
            normalize_spectra: whether to divide each row of `SampleSet.spectra` by each row's sum
            normalize_sources: whether to divide each row of `SampleSet.sources` by each row's sum
            verbose: whether to show detailed output

        Returns:
            `SampleSet` containing foreground and/or background seeds generated by GADRAS
        """
        if isinstance(config, str):
            with open(config, "r") as stream:
                config = yaml.safe_load(stream)
        elif not isinstance(config, dict):
            msg = (
                "The provided config for seed synthesis must either be "
                "a path to a properly structured YAML file or "
                "a properly structured dictionary."
            )
            raise ValueError(msg)

        validate_inject_config(config)

        setups = get_inject_setups(config)
        with self._cwd(GADRAS_ASSEMBLY_PATH):
            gadras_api = get_gadras_api()
            detector_name = config["gamma_detector"]["name"]
            gadras_api.detectorSetCurrent(detector_name)
            original_detector_parameters = self._get_detector_parameters(gadras_api)
            if verbose:
                print(f"Obtaining sources for '{detector_name}'")

            source_list = []
            for s in setups:
                source_injector = SourceInjector(gadras_api)
                new_detector_parameters = s["gamma_detector"]["parameters"]
                now = _get_utc_timestamp().replace(":", "_")  # replace() prevents error on Windows
                rel_output_path = f"{now}_sources.pcf"
                try:
                    self._set_detector_parameters(gadras_api, new_detector_parameters, verbose)
                    # TODO: propagate dry_run to injectors
                    # Source injects
                    pcf_abs_path = source_injector.generate(s, rel_output_path, verbose=verbose)
                    seeds_ss = read_pcf(pcf_abs_path)
                    # Manually set distance_cm so it works with expanded configs
                    seeds_ss.info["distance_cm"] = s["gamma_detector"]["parameters"]["distance_cm"]
                    if not normalize_sources:
                        seeds_ss.sources *= seeds_ss.spectra.sum(axis=1).values
                    if normalize_spectra:
                        seeds_ss.normalize()
                    source_list.append(seeds_ss)
                except Exception as e:
                    # Try to restore .dat file to original state even when an error occurs
                    self._set_detector_parameters(gadras_api, original_detector_parameters)
                    raise e

            # Restore .dat file to original state
            self._set_detector_parameters(gadras_api, original_detector_parameters)

        ss = SampleSet()
        ss.concat(source_list)
        ss.detector_info = deepcopy(config["gamma_detector"])
        ss.set_dead_time_proportions()

        return ss


class SeedMixer():
    """Randomly mixes seeds in a `SampleSet` """
    def __init__(self, seeds_ss: SampleSet, mixture_size: int = 2, dirichlet_alpha: float = 2.0,
                 restricted_isotope_pairs: List[Tuple[str, str]] = [], rng: Generator = None):
        """
        Args:
            seeds_ss: `SampleSet` of `n` seed spectra where `n` >= `mixture_size`
            mixture_size: number of templates to mix
            dirichlet_alpha: Dirichlet parameter controlling the nature of proportions
            restricted_pairs: list of 2-tuples containing pairs of isotope strings that
                are not to be mixed together
            rng: NumPy random number generator, useful for experiment repeatability

        Raises:
            AssertionError when `mixture_size` is less than 2
        """
        assert mixture_size >= 2

        self.seeds_ss = seeds_ss
        self.mixture_size = mixture_size
        self.dirichlet_alpha = dirichlet_alpha
        self.restricted_isotope_pairs = restricted_isotope_pairs
        if rng is None:
            self.rng = np.random.default_rng()
        else:
            self.rng = rng

        self._check_seeds()

    def _check_seeds(self, skip_health_check: bool = False):
        if not skip_health_check:
            self.seeds_ss.check_seed_health()
        n_sources_per_row = np.count_nonzero(
            self.seeds_ss.get_source_contributions(),
            axis=1
        )
        if not np.all(n_sources_per_row == 1):
            raise ValueError("At least one provided seed contains a mixture of sources.")
        if np.any(np.count_nonzero(self.seeds_ss.get_source_contributions(), axis=1) == 0):
            raise ValueError("At least one provided seed contains no ground truth.")
        for ecal_column in self.seeds_ss.ECAL_INFO_COLUMNS:
            all_ecal_columns_close_to_one = np.all(np.isclose(
                self.seeds_ss.info[ecal_column],
                self.seeds_ss.info[ecal_column][0]
            ))
            if not all_ecal_columns_close_to_one:
                raise ValueError((
                    f"{ecal_column} is not consistent. "
                    "All seeds must have the same energy calibration."
                ))

    def __call__(self, n_samples: int, max_batch_size: int = 100,
                 skip_health_check: bool = False) -> Iterator[SampleSet]:
        """Yields batches of seeds one at a time until a specified number of samples has
        been reached.

        Dirichlet intuition:

        - Higher alpha: values will converge on 1/N where N is mixture size
        - Lower alpha: values will converge on ~0 but there will be a single 1

        Using `np.random.dirichlet` with too small of an alpha will result in NaNs
            (per https://github.com/rust-random/rand/pull/1209)
        Using `numpy.random.Generator.dirichlet` instead avoids this.

        TODO: seed-level restrictions

        Args:
            n_samples: total number of mixture seeds to produce across all batches
            max_batch_size: maximum size of a batch per yield
            skip_health_check: whether to skip the seed health check

        Returns:
            Generator of `SampleSet`s
        """
        self._check_seeds(skip_health_check)

        isotope_to_seeds = self.seeds_ss.sources_columns_to_dict(target_level="Isotope")
        isotopes = list(isotope_to_seeds.keys())
        seeds = list(isotope_to_seeds.values())  # not necessarily distinct
        seeds = [item for sublist in seeds for item in sublist]
        n_seeds = len(seeds)
        n_distinct_seeds = len(set(seeds))
        if n_distinct_seeds != n_seeds:
            raise ValueError("Seed names must be unique.")
        isotope_probas = list([len(isotope_to_seeds[i]) / n_seeds for i in isotopes])
        spectra_row_labels = self.seeds_ss.sources.idxmax(axis=1)
        restricted_isotope_bidict = bidict({k: v for k, v in self.restricted_isotope_pairs})
        seed_spectra_df = self.seeds_ss.spectra.copy()
        seed_spectra_df.index = spectra_row_labels

        try:
            _ = iter(self.dirichlet_alpha)
        except TypeError:
            seed_to_alpha = {s: self.dirichlet_alpha for s in seeds}
        else:
            if n_seeds != len(self.dirichlet_alpha):
                raise ValueError("Number of Dirichlet alphas does not equal the number of seeds.")
            seed_to_alpha = {s: a for s, a in zip(seeds, self.dirichlet_alpha)}

        n_samples_produced = 0
        while n_samples_produced < n_samples:
            batch_size = n_samples - n_samples_produced
            if batch_size > max_batch_size:
                batch_size = max_batch_size
            # Make batch
            isotope_choices = [
                get_choices(
                    [],
                    isotopes.copy(),
                    np.array(isotope_probas.copy()),
                    restricted_isotope_bidict,
                    self.mixture_size,
                    self.rng,
                )
                for _ in range(batch_size)
            ]
            seed_choices = [
                [isotope_to_seeds[i][self.rng.choice(len(isotope_to_seeds[i]))] for i in c]
                for c in isotope_choices
            ]
            batch_dirichlet_alphas = np.array([
                [seed_to_alpha[i] for i in s]
                for s in seed_choices
            ])
            seed_ratios = [
                self.rng.dirichlet(
                    alpha=alpha
                ) for alpha in batch_dirichlet_alphas
            ]

            # Compute the spectra
            spectra = np.array([
                (seed_ratios[i] * seed_spectra_df.loc[m].T).sum(axis=1)
                for i, m in enumerate(seed_choices)
            ])

            # Build SampleSet
            batch_ss = SampleSet()
            batch_ss.detector_info = self.seeds_ss.detector_info
            batch_ss.spectra_state = self.seeds_ss.spectra_state
            batch_ss.spectra_type = self.seeds_ss.spectra_type
            batch_ss.spectra = pd.DataFrame(spectra)
            batch_ss.info = pd.DataFrame(
                [self.seeds_ss.info.iloc[0].values] * batch_size,
                columns=self.seeds_ss.info.columns
            )
            batch_sources_dfs = []
            for r, s in zip(seed_ratios, seed_choices):
                sources_cols = pd.MultiIndex.from_tuples(
                    s,
                    names=SampleSet.SOURCES_MULTI_INDEX_NAMES,
                )
                sources_df = pd.DataFrame([r], columns=sources_cols)
                batch_sources_dfs.append(sources_df)
            sources_df = pd\
                .concat(batch_sources_dfs)\
                .fillna(0.0)
            batch_ss.sources = sources_df.reindex(
                columns=self.seeds_ss.sources.columns,
                fill_value=0.0
            )

            n_samples_produced += batch_size

            yield batch_ss

    def generate(self, n_samples: int, max_batch_size: int = 100,
                 skip_health_check: bool = False) -> SampleSet:
        """Computes random mixtures of seeds at the isotope level.
        """
        batches = []
        batch_iterable = self(
            n_samples,
            max_batch_size=max_batch_size,
            skip_health_check=skip_health_check
        )
        for batch_ss in batch_iterable:
            batches.append(batch_ss)
        mixtures_ss = SampleSet()
        mixtures_ss.spectra_type = self.seeds_ss.spectra_type
        mixtures_ss.concat(batches)

        return mixtures_ss


class bidict(dict):
    """Bi-directional hash table to perform efficient reverse lookups.

        Source: https://stackoverflow.com/a/21894086
    """
    def __init__(self, *args, **kwargs):
        super(bidict, self).__init__(*args, **kwargs)
        self.inverse = {}
        for key, value in self.items():
            self.inverse.setdefault(value, []).append(key)

    def __setitem__(self, key, value):
        if key in self:
            self.inverse[self[key]].remove(key)
        super(bidict, self).__setitem__(key, value)
        self.inverse.setdefault(value, []).append(key)

    def __delitem__(self, key):
        self.inverse.setdefault(self[key], []).remove(key)
        if self[key] in self.inverse and not self.inverse[self[key]]:
            del self.inverse[self[key]]
        super(bidict, self).__delitem__(key)


def get_choices(choices_so_far: list, options: list, options_probas: np.array,
                restricted_pairs: bidict, n_choices_remaining: int, rng: Generator = None):
    """Makes a random choice from the given options until the desired number of choices
    is reached.

    After a choice is made, future options are adjusted as follows:

    - The current choice itself is excluded
    - If the current choice is not allowed to co-exist with other options,
        those options are also exclude

    Args:
        choices_so_far: list being build up over time with random choices from `options`
        options: list being reduced over time as choices and restricted choices are removed
        options_probas: probability assigned to each option
        restricted_pairs: bi-directional hash table allowing us to quickly find restrictions
            regardless of the order in which the pair has been specified
        n_choices_remaining: number of choices remaining
        rng: NumPy random number generator, useful for experiment repeatability

    Raises:
        `ValueError` when the number of choices desired exceeds the number of options available
    """
    if n_choices_remaining == 0:
        return choices_so_far
    elif len(options) < n_choices_remaining:
        raise ValueError("There are not enough options to achieve the specified number of choices.")

    if not rng:
        rng = np.random.default_rng()

    choice = rng.choice(a=options, replace=False, p=options_probas)
    choices_so_far.append(choice)

    # Remove current choice from future options
    choice_index = options.index(choice)
    del options[choice_index]
    options_probas = np.delete(options_probas, choice_index)

    # If the current choice places restrictions on future options, then get those out too
    restricted_choices = []
    if choice in restricted_pairs:
        restricted_choices = [restricted_pairs[choice]]
    elif choice in restricted_pairs.inverse:
        restricted_choices = restricted_pairs.inverse[choice]

    relevant_restrictions = [rc for rc in restricted_choices if rc in options]
    for rc in relevant_restrictions:
        restricted_choice_index = options.index(rc)
        del options[restricted_choice_index]
        options_probas = np.delete(options_probas, restricted_choice_index)

    # Re-normalize probabilities
    options_probas = options_probas / options_probas.sum()

    n_choices_remaining -= 1
    return get_choices(choices_so_far, options, options_probas, restricted_pairs,
                       n_choices_remaining, rng)
