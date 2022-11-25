# Copyright 2021 National Technology & Engineering Solutions of Sandia, LLC (NTESS).
# Under the terms of Contract DE-NA0003525 with NTESS,
# the U.S. Government retains certain rights in this software.
"""This modules contains utilities for generating synthetic gamma spectrum templates from GADRAS."""
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Tuple, Union

import numpy as np
import pandas as pd
import yaml

from riid.data import SampleSet
from riid.data.labeling import BACKGROUND_LABEL, label_to_index_element
from riid.data.sampleset import read_pcf
from riid.gadras.api import (DETECTOR_PARAMS, GADRAS_ASSEMBLY_PATH,
                             INJECT_PARAMS, BackgroundInjector, SourceInjector,
                             get_counts_per_bg_source_unit, get_gadras_api,
                             validate_inject_config)


class SeedSynthesizer():

    @contextmanager
    def _cwd(self, path):
        """ Temporarily changes working directory.

            This is used to change the execution location which necessary due to how the GADRAS API
            uses relative pathing from its installation directory.
        """
        oldpwd = os.getcwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(oldpwd)

    def _get_kut_and_cosmic_sources(self, k_pct, u_ppm, t_ppm, cosmic, counts,
                                    k_counts_per_pct, u_counts_per_ppm, t_counts_per_ppm,
                                    cosmic_counts):
        """Assumes a live time of 1 second.
        """
        terrestrial_counts = counts - cosmic_counts if cosmic else counts

        expected_k_counts = k_counts_per_pct * k_pct
        expected_u_counts = u_counts_per_ppm * u_ppm
        expected_t_counts = t_counts_per_ppm * t_ppm
        expected_counts = expected_k_counts + expected_u_counts + expected_t_counts
        expected_k_ratio = expected_k_counts / expected_counts
        expected_u_ratio = expected_u_counts / expected_counts
        expected_t_ratio = expected_t_counts / expected_counts

        # rescaling
        actual_k_counts = round(expected_k_ratio * terrestrial_counts)
        actual_u_counts = round(expected_u_ratio * terrestrial_counts)
        actual_t_counts = round(expected_t_ratio * terrestrial_counts)

        sources = {
            "K40": actual_k_counts,
            "Ra226": actual_u_counts,
            "Th232": actual_t_counts,
            "Cosmic": cosmic_counts if cosmic else 0,
        }

        return sources

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

    def generate(self, config: Union[str, dict], normalize_sources=True,
                 dry_run=False, verbose: bool = False) -> Tuple[SampleSet, SampleSet]:
        """Produces a SampleSet containing foreground and/or background seeds using GADRAS based
        on the given inject configuration.

        Args:
            config: a dictionary is treated as the actual config containing the needed information
                to perform injects via the GADRAS API, while a string is treated as a path to a YAML
                file which deserialized as a dictionary.
            normalize_sources: whether to divide each row of the SampleSet's sources
                DataFrame by its sum. Defaults to True.
            dry_run: when False, actually performs inject(s), otherwise simply reports info about
                what would happen.  Defaults to False.
            verbose: when True, displays extra output.

        Returns:
            A SampleSet containing foreground and/or background seeds generated by GADRAS.
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

        with self._cwd(GADRAS_ASSEMBLY_PATH):
            gadras_api = get_gadras_api()
            detector_name = config["gamma_detector"]["name"]
            new_detector_parameters = config["gamma_detector"]["parameters"]
            gadras_api.detectorSetCurrent(detector_name)
            original_detector_parameters = self._get_detector_parameters(gadras_api)
            now = datetime.utcnow().isoformat().replace(":", "_")

            rel_fg_output_path = f"{now}_fg.pcf"
            rel_bg_output_path = f"{now}_bg.pcf"
            fg_list = []
            bg_list = []
            detector_setups = [new_detector_parameters]  # TODO: generate all detector_setups
            source_injector = SourceInjector(gadras_api)
            background_injector = BackgroundInjector(gadras_api)
            try:
                for d in detector_setups:
                    self._set_detector_parameters(gadras_api, d, verbose, dry_run)

                    if dry_run:
                        continue

                    # TODO: propagate dry_run to injectors

                    # Source injects
                    if verbose:
                        print('Obtaining sources...')
                    fg_pcf_abs_path = source_injector.generate(
                        config,
                        rel_fg_output_path,
                        verbose=verbose
                    )
                    fg_seeds_ss = read_pcf(fg_pcf_abs_path)
                    fg_seeds_ss.normalize()
                    if normalize_sources:
                        fg_seeds_ss.normalize_sources()
                    fg_list.append(fg_seeds_ss)

                    # Background injects
                    if verbose:
                        print('Obtaining backgrounds...')
                    bg_pcf_abs_path = background_injector.generate(
                        config,
                        rel_bg_output_path,
                        verbose=verbose
                    )
                    bg_seeds_ss = read_pcf(bg_pcf_abs_path)

                    # Calculate ground truth for backgrounds
                    worker = gadras_api.GetBatchInjectWorker()
                    bg_counts_params = (gadras_api, new_detector_parameters, worker)
                    k_counts_per_pct = get_counts_per_bg_source_unit(*bg_counts_params, "K")
                    u_counts_per_ppm = get_counts_per_bg_source_unit(*bg_counts_params, "U")
                    t_counts_per_ppm = get_counts_per_bg_source_unit(*bg_counts_params, "T")
                    cosmic_counts = get_counts_per_bg_source_unit(*bg_counts_params, "Cosmic")
                    sources = []
                    for i in range(bg_seeds_ss.n_samples):
                        i_counts = round(bg_seeds_ss.spectra.iloc[i].sum())
                        i_bg_config = config["backgrounds"][i]
                        i_sources = self._get_kut_and_cosmic_sources(
                            i_bg_config["K40_percent"],
                            i_bg_config["U_ppm"],
                            i_bg_config["Th232_ppm"],
                            i_bg_config["cosmic"],
                            i_counts,
                            k_counts_per_pct,
                            u_counts_per_ppm,
                            t_counts_per_ppm,
                            cosmic_counts
                        )
                        sources.append(i_sources)
                    bg_sources_df = pd.DataFrame(sources)
                    bg_sources_df.columns = pd.MultiIndex.from_tuples(
                        [label_to_index_element(x, label_level="Seed")
                         for x in bg_sources_df.columns],
                        names=SampleSet.SOURCES_MULTI_INDEX_NAMES
                    )
                    bg_seeds_ss.sources = bg_sources_df

                    bg_seeds_ss.normalize()
                    if normalize_sources:
                        bg_seeds_ss.normalize_sources()

                    bg_list.append(bg_seeds_ss)

                if dry_run:
                    return None

            except Exception as e:
                # Try to restore .dat file to original state even when an error occurs
                if not dry_run:
                    self._set_detector_parameters(gadras_api, original_detector_parameters)
                raise e

            # Restore .dat file to original state
            if not dry_run:
                self._set_detector_parameters(gadras_api, original_detector_parameters)

        all_fg_seeds_ss = SampleSet()
        all_fg_seeds_ss.concat(fg_list)
        all_fg_seeds_ss.detector_info = config["gamma_detector"]
        all_bg_seeds_ss = SampleSet()
        all_bg_seeds_ss.concat(bg_list)
        all_fg_seeds_ss.detector_info = config["gamma_detector"]

        return all_fg_seeds_ss, all_bg_seeds_ss


class SeedMixer():
    def __init__(self, mixture_size: int = 2, min_source_contribution: float = 0.1):
        assert mixture_size >= 2
        assert min_source_contribution >= 0.1
        assert mixture_size * min_source_contribution < 1.0

        self.mixture_size = mixture_size
        self.min_source_contribution = min_source_contribution

    def generate(self, seeds_ss: SampleSet, n_samples: int = 10000) -> SampleSet:
        """Computes random mixtures of seeds across the isotope level.

            n_mixture = seed_1 * ratio_1 + seed_2 * ratio_2 + ... + seed_n * ratio_n
                where:
                - ratio_1 + ratio_2 + ... + ratio_n = 1
                - sum(seed_i) = 1
                - sum(n_mixture) = self.mixture_size
                  (this is before re-normalizing, at which point it will sum to 1)

            For 3 contributors:
                running_contribution = 0.0
                contribution1 = uniform(min_contribution, 1 - running_contribution
                                - n_remaining_contributors * min_contribution)
                              = uniform(0.1, 1 - 0.0 - 2 * 0.1)
                              = uniform(0.1, 0.8)
                              = 0.80
                running_contribution += contribution1
                contribution2 = uniform(0.1, 1 - running_contribution - 1 * 0.1)
                              = uniform(0.1, 0.1)
                              = 0.10
                running_contribution += contribution2
                contribution3 = 1 - running_contribution
        """
        if seeds_ss and not seeds_ss.all_spectra_sum_to_one():
            raise ValueError("At least one provided seed does not sum close to 1.")

        if not np.all(np.count_nonzero(seeds_ss.get_source_contributions().values, axis=1) == 1):
            raise ValueError("At least one provided seed contains mixture of sources.")

        for ecal_column in seeds_ss.ECAL_INFO_COLUMNS:
            if not np.all(np.isclose(seeds_ss.info[ecal_column], seeds_ss.info[ecal_column][0])):
                raise ValueError("At least one ecal value is different than the others.")

        non_bg_seeds_ss = seeds_ss[seeds_ss.get_labels() != BACKGROUND_LABEL]
        non_bg_seeds_ss.sources.drop(
            BACKGROUND_LABEL,
            axis=1,
            level="Isotope",
            inplace=True,
            errors='ignore'
        )
        isotopes = non_bg_seeds_ss.get_labels().values
        n_sources = non_bg_seeds_ss.n_samples
        unique_isotopes, indices = np.unique(isotopes, return_index=True)
        n_isotopes = len(unique_isotopes)

        # preserve original order of isotopes (np.unique() sorts them)
        unique_isotopes = np.array([isotopes[i] for i in sorted(indices)])
        cnts = np.array([np.count_nonzero(isotopes == isotope) for isotope in unique_isotopes])

        isotope_inds = [np.arange(cnts[:idx].sum(), cnts[:idx+1].sum())
                        for idx in range(n_isotopes)]
        isotope_dict = dict(zip(unique_isotopes, isotope_inds))
        mixture_inds = {i+1: [] for i in range(self.mixture_size)}
        mixture_inds[1] = [(each,) for each in range(n_sources)]

        # first generate mixture indices
        for n in range(2, self.mixture_size+1):
            for mix_idx in mixture_inds[n-1]:
                # get first source index for next isotope after last isotope in the mixture
                last_isotope = isotopes[mix_idx[-1]]
                if last_isotope != unique_isotopes[-1]:
                    next_source = np.where(unique_isotopes == last_isotope)[0].squeeze() + 1
                    next_isotope = unique_isotopes[next_source]
                    next_idx = isotope_dict[next_isotope][0]

                    # generate next set of mixture indices
                    mixtures = [(*mix_idx, each) for each in range(next_idx, n_sources)]
                    mixture_inds[n].extend(mixtures)

        # generate sampling probability distribution to accomadate mixture balancing
        # at isotope level
        flat_mixture_inds = [item for sublist in mixture_inds[self.mixture_size]
                             for item in sublist]
        flat_mixture_sources = [isotopes[each] for each in flat_mixture_inds]
        unique_isotopes_sorted, isotope_occurences = np.unique(flat_mixture_sources,
                                                               return_counts=True)
        isotope_weights = 1/isotope_occurences
        isotope_weights_dict = dict(zip(unique_isotopes_sorted, isotope_weights))

        mixture_weights = np.zeros(len(mixture_inds[self.mixture_size]))
        for idx, mixture in enumerate(mixture_inds[self.mixture_size]):
            mixture_weights[idx] = sum([isotope_weights_dict[isotopes[ind]] for ind in mixture])
        mixture_weights = mixture_weights/mixture_weights.sum()  # normalize to make pdf

        # randomly sample mixtures
        random_seed_inds = np.random.choice(len(mixture_weights),
                                            size=n_samples,
                                            replace=True,
                                            p=mixture_weights)
        random_isotopes = list(sum([mixture_inds[self.mixture_size][each]
                               for each in random_seed_inds], ()))
        random_isotopes = [isotopes[each] for each in random_isotopes]

        mixture_seeds = np.zeros((n_samples, seeds_ss.n_channels))
        source_matrix = np.zeros((n_samples, n_sources))

        # create mixture seeds
        for idx, random_mixture_ind in enumerate(random_seed_inds):
            # randomly sample probability distribution
            ratios = [0.0 for i in range(self.mixture_size)]
            for ratio_idx in range(self.mixture_size - 1):
                ratios[ratio_idx] = np.random.uniform(self.min_source_contribution,
                                                      1 - sum(ratios) -
                                                      (self.mixture_size - ratio_idx + 1) *
                                                      self.min_source_contribution)
            ratios[-1] = 1.0 - sum(ratios)

            # generate mixture data
            mixture = mixture_inds[self.mixture_size][random_mixture_ind]
            source_contributions = np.zeros(n_sources)
            for ratio_idx, spectra_idx in enumerate(mixture):
                source_contribution = seeds_ss.spectra.values[spectra_idx, :]\
                    * ratios[ratio_idx]
                mixture_seeds[idx, :] += source_contribution
                source_contributions[spectra_idx] = ratios[ratio_idx]
            source_matrix[idx, :] = source_contributions

        source_matrix = np.array(source_matrix)

        mixture_ss = SampleSet()
        mixture_ss.spectra = pd.DataFrame(
            mixture_seeds
        )
        mixture_ss.sources = pd.DataFrame(
            source_matrix,
            columns=non_bg_seeds_ss.sources.columns
        )

        # populate SampleSet info
        mixture_ss.info = pd.DataFrame(
            np.full((mixture_ss.spectra.shape[0], seeds_ss.info.shape[1]), None),
            columns=seeds_ss.info.columns
        )

        for ecal_column in seeds_ss.ECAL_INFO_COLUMNS:
            mixture_ss.info.loc[:, ecal_column] = seeds_ss.info[ecal_column][0]
        mixture_ss.info.loc[:, 'tag'] = seeds_ss.info['tag'][0]

        # TODO: fill in rest of columns
        # description, timestamp, live_time, real_time, snr_target, snr_estimate, sigma, bg_counts,
        # fg_counts, bg_counts_expected, fg_counts_expected, total_counts, total_neutron_counts,
        # distance_cm, area_density, atomic_number

        return mixture_ss
