import argparse
import json
import os
from pathlib import Path
import yaml
from ast import literal_eval
import copy

import bilby
import bilby.core
import matplotlib
import numpy as np
from scipy.interpolate import interp1d
import pandas as pd
from astropy import time
from bilby.core.likelihood import ZeroLikelihood

from ..utils.models import refresh_models_list
from .injection import create_light_curve_data
from .likelihood import OpticalLightCurve
from .model import create_light_curve_model_from_args, model_parameters_dict
from .prior import create_prior_from_args
from .utils import getFilteredMag, dataProcess
from .io import loadEvent

matplotlib.use("agg")


def get_parser(**kwargs):
    add_help = kwargs.get("add_help", True)

    parser = argparse.ArgumentParser(
        description="Inference on kilonova ejecta parameters.",
        add_help=add_help,
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Name of the configuration file containing parameter values.",
    )
    parser.add_argument(
        "--model", type=str, help="Name of the kilonova model to be used"
    )
    parser.add_argument(
        "--interpolation-type",
        type=str,
        help="SVD interpolation scheme.",
        default="sklearn_gp",
    )
    parser.add_argument(
        "--svd-path",
        type=str,
        help="Path to the SVD directory with {model}.joblib",
        default="svdmodels",
    )
    parser.add_argument(
        "--outdir",
        type=str,
        help="Path to the output directory",
        default="outdir",
    )
    parser.add_argument(
        "--label", type=str, help="Label for the run", default="injection"
    )
    parser.add_argument(
        "--trigger-time",
        type=float,
        help="Trigger time in modified julian day, not required if injection set is provided",
    )
    parser.add_argument(
        "--data",
        type=str,
        help="Path to the data file in [time(isot) filter magnitude error] format",
    )
    parser.add_argument("--prior", type=str, help="Path to the prior file")
    parser.add_argument(
        "--tmin",
        type=float,
        default=0.05,
        help="Days to start analysing from the trigger time (default: 0.05)",
    )
    parser.add_argument(
        "--tmax",
        type=float,
        default=14.0,
        help="Days to stop analysing from the trigger time (default: 14)",
    )
    parser.add_argument(
        "--dt", type=float, default=0.1, help="Time step in day (default: 0.1)"
    )
    parser.add_argument(
        "--log-space-time",
        action="store_true",
        default=False,
        help="Create the sample_times to be uniform in log-space",
    )
    parser.add_argument(
        "--n-tstep",
        type=int,
        default=50,
        help="Number of time steps (used with --log-space-time, default: 50)",
    )
    parser.add_argument(
        "--photometric-error-budget",
        type=float,
        default=0.1,
        help="Photometric error (mag) (default: 0.1)",
    )
    parser.add_argument(
        "--svd-mag-ncoeff",
        type=int,
        default=10,
        help="Number of eigenvalues to be taken for mag evaluation (default: 10)",
    )
    parser.add_argument(
        "--svd-lbol-ncoeff",
        type=int,
        default=10,
        help="Number of eigenvalues to be taken for lbol evaluation (default: 10)",
    )
    parser.add_argument(
        "--filters",
        type=str,
        help="A comma seperated list of filters to use (e.g. g,r,i). If none is provided, will use all the filters available",
    )
    parser.add_argument(
        "--use-Ebv",
        action="store_true",
        default=False,
        help="If using the Ebv extinction during the inference",
    )
    parser.add_argument(
        "--Ebv-max",
        type=float,
        default=0.5724,
        help="Maximum allowed value for Ebv (default:0.5724)",
    )
    parser.add_argument(
        "--grb-resolution",
        type=float,
        default=5,
        help="The upper bound on the ratio between thetaWing and thetaCore (default: 5)",
    )
    parser.add_argument(
        "--jet-type",
        type=int,
        default=0,
        help="Jet type to used used for GRB afterglow light curve (default: 0)",
    )
    parser.add_argument(
        "--error-budget",
        type=str,
        default="1.0",
        help="Additional systematic error (mag) to be introduced (default: 1)",
    )
    parser.add_argument(
        "--sampler",
        type=str,
        default="pymultinest",
        help="Sampler to be used (default: pymultinest)",
    )
    parser.add_argument(
        "--soft-init",
        action="store_true",
        default=False,
        help="To start the sampler softly (without any checking, default: False)",
    )
    parser.add_argument(
        "--sampler-kwargs",
        default="{}",
        type=str,
        help="Additional kwargs (e.g. {'evidence_tolerance':0.5}) for bilby.run_sampler, put a double quotation marks around the dictionary",
    )
    parser.add_argument(
        "--cpus",
        type=int,
        default=1,
        help="Number of cores to be used, only needed for dynesty (default: 1)",
    )
    parser.add_argument(
        "--nlive", type=int, default=2048, help="Number of live points (default: 2048)"
    )
    parser.add_argument(
        "--reactive-sampling",
        action="store_true",
        default=False,
        help="To use reactive sampling in ultranest (default: False)",
    )
    parser.add_argument(
        "--seed",
        metavar="seed",
        type=int,
        default=42,
        help="Sampling seed (default: 42)",
    )
    parser.add_argument(
        "--injection", metavar="PATH", type=str, help="Path to the injection json file"
    )
    parser.add_argument(
        "--injection-num",
        metavar="eventnum",
        type=int,
        help="The injection number to be taken from the injection set",
    )
    parser.add_argument(
        "--injection-detection-limit",
        metavar="mAB",
        type=str,
        help="The highest mAB to be presented in the injection data set, any mAB higher than this will become a non-detection limit. Should be comma delimited list same size as injection set.",
    )
    parser.add_argument(
        "--injection-outfile",
        metavar="PATH",
        type=str,
        help="Path to the output injection lightcurve",
    )
    parser.add_argument(
        "--injection-model",
        type=str,
        help="Name of the kilonova model to be used for injection (default: the same as model used for recovery)",
    )
    parser.add_argument(
        "--remove-nondetections",
        action="store_true",
        default=False,
        help="remove non-detections from fitting analysis",
    )
    parser.add_argument(
        "--detection-limit",
        metavar="DICT",
        type=str,
        default=None,
        help="Dictionary for detection limit per filter, e.g., {'r':22, 'g':23}, put a double quotation marks around the dictionary",
    )
    parser.add_argument(
        "--with-grb-injection",
        help="If the injection has grb included",
        action="store_true",
    )
    parser.add_argument(
        "--prompt-collapse",
        help="If the injection simulates prompt collapse and therefore only dynamical",
        action="store_true",
    )
    parser.add_argument(
        "--ztf-sampling", help="Use realistic ZTF sampling", action="store_true"
    )
    parser.add_argument(
        "--ztf-uncertainties",
        help="Use realistic ZTF uncertainties",
        action="store_true",
    )
    parser.add_argument(
        "--ztf-ToO",
        help="Adds realistic ToO observations during the first one or two days. Sampling depends on exposure time specified. Valid values are 180 (<1000sq deg) or 300 (>1000sq deg). Won't work w/o --ztf-sampling",
        type=str,
        choices=["180", "300"],
    )
    parser.add_argument(
        "--train-stats",
        help="Creates a file too.csv to derive statistics",
        action="store_true",
    )
    parser.add_argument(
        "--rubin-ToO",
        help="Adds ToO obeservations based on the strategy presented in arxiv.org/abs/2111.01945.",
        action="store_true",
    )
    parser.add_argument(
        "--rubin-ToO-type",
        help="Type of ToO observation. Won't work w/o --rubin-ToO",
        type=str,
        choices=["BNS", "NSBH"],
    )
    parser.add_argument(
        "--xlim",
        type=str,
        default="0,14",
        help="Start and end time for light curve plot (default: 0-14)",
    )
    parser.add_argument(
        "--ylim",
        type=str,
        default="22,16",
        help="Upper and lower magnitude limit for light curve plot (default: 22-16)",
    )
    parser.add_argument(
        "--generation-seed",
        metavar="seed",
        type=int,
        default=42,
        help="Injection generation seed (default: 42)",
    )
    parser.add_argument(
        "--plot", action="store_true", default=False, help="add best fit plot"
    )
    parser.add_argument(
        "--bilby-zero-likelihood-mode",
        action="store_true",
        default=False,
        help="enable prior run",
    )
    parser.add_argument(
        "--photometry-augmentation",
        help="Augment photometry to improve parameter recovery",
        action="store_true",
    )
    parser.add_argument(
        "--photometry-augmentation-seed",
        metavar="seed",
        type=int,
        default=0,
        help="Optimal generation seed (default: 0)",
    )
    parser.add_argument(
        "--photometry-augmentation-N-points",
        help="Number of augmented points to include",
        type=int,
        default=10,
    )
    parser.add_argument(
        "--photometry-augmentation-filters",
        type=str,
        help="A comma seperated list of filters to use for augmentation (e.g. g,r,i). If none is provided, will use all the filters available",
    )
    parser.add_argument(
        "--photometry-augmentation-times",
        type=str,
        help="A comma seperated list of times to use for augmentation in days post trigger time (e.g. 0.1,0.3,0.5). If none is provided, will use random times between tmin and tmax",
    )

    parser.add_argument(
        "--conditional-gaussian-prior-thetaObs",
        action="store_true",
        default=False,
        help="The prior on the inclination is against to a gaussian prior centered at zero with sigma = thetaCore / N_sigma",
    )

    parser.add_argument(
        "--conditional-gaussian-prior-N-sigma",
        default=1,
        type=float,
        help="The input for N_sigma; to be used with conditional-gaussian-prior-thetaObs set to True",
    )

    parser.add_argument(
        "--sample-over-Hubble",
        action="store_true",
        default=False,
        help="To sample over Hubble constant and redshift",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="print out log likelihoods",
    )

    parser.add_argument(
        "--refresh-models-list",
        type=bool,
        default=False,
        help="Refresh the list of models available on Gitlab",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        default=False,
        help="only look for local svdmodels (ignore Gitlab)",
    )

    parser.add_argument(
        "--bestfit",
        help="Save the best fit parameters and magnitudes to JSON",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "--fits-file",
        help="Fits file output from Bayestar, to be used for constructing dL-iota prior"
    )
    parser.add_argument(
        "--cosiota-node-num",
        help="Number of cos-iota nodes used in the Bayestar fits (default: 10)",
        default=10,
    )

    parser.add_argument(
        "--skip-sampling",
        help="If analysis has already run, skip bilby sampling and compute results from checkpoint files. Combine with --plot to make plots from these files.",
        action="store_true",
        default=False,
    )
    return parser


def analysis(args):

    if args.sampler == "pymultinest":
        if len(args.outdir) > 64:
            print(
                "WARNING: output directory name is too long, it should not be longer than 64 characters"
            )
            exit()

    refresh = False
    try:
        refresh = args.refresh_models_list
    except AttributeError:
        pass
    if refresh:
        refresh_models_list(
            models_home=args.svd_path if args.svd_path not in [None, ""] else None
        )

    bilby.core.utils.setup_logger(outdir=args.outdir, label=args.label)
    bilby.core.utils.check_directory_exists_and_if_not_mkdir(args.outdir)

    # initialize light curve model
    if args.log_space_time:
        if args.n_tstep:
            n_step = args.n_tstep
        else:
            n_step = int((args.tmax - args.tmin) / args.dt)
        sample_times = np.logspace(
            np.log10(args.tmin), np.log10(args.tmax + args.dt), n_step
        )
    else:
        sample_times = np.arange(args.tmin, args.tmax + args.dt, args.dt)
    print("Creating light curve model for inference")

    if args.filters:
        filters = args.filters.replace(" ", "")  # remove all whitespace
        filters = filters.split(",")
        if len(filters) == 0:
            raise ValueError("Need at least one valid filter.")
    else:
        filters = None

    # create the kilonova data if an injection set is given
    if args.injection:
        with open(args.injection, "r") as f:
            injection_dict = json.load(
                f, object_hook=bilby.core.utils.decode_bilby_json
            )
        injection_df = injection_dict["injections"]
        injection_parameters = injection_df.iloc[args.injection_num].to_dict()

        if "geocent_time" in injection_parameters:
            tc_gps = time.Time(injection_parameters["geocent_time"], format="gps")
        elif "geocent_time_x" in injection_parameters:
            tc_gps = time.Time(injection_parameters["geocent_time_x"], format="gps")
        else:
            print("Need either geocent_time or geocent_time_x")
            exit(1)
        trigger_time = tc_gps.mjd

        injection_parameters["kilonova_trigger_time"] = trigger_time
        if args.prompt_collapse:
            injection_parameters["log10_mej_wind"] = -3.0

        # sanity check for eject masses
        if "log10_mej_dyn" in injection_parameters and not np.isfinite(
            injection_parameters["log10_mej_dyn"]
        ):
            injection_parameters["log10_mej_dyn"] = -3.0
        if "log10_mej_wind" in injection_parameters and not np.isfinite(
            injection_parameters["log10_mej_wind"]
        ):
            injection_parameters["log10_mej_wind"] = -3.0

        args.kilonova_tmin = args.tmin
        args.kilonova_tmax = args.tmax
        args.kilonova_tstep = args.dt
        args.kilonova_error = args.photometric_error_budget

        if not args.injection_model:
            args.kilonova_injection_model = args.model
        else:
            args.kilonova_injection_model = args.injection_model
        args.kilonova_injection_svd = args.svd_path
        args.injection_svd_mag_ncoeff = args.svd_mag_ncoeff
        args.injection_svd_lbol_ncoeff = args.svd_lbol_ncoeff

        print("Creating injection light curve model")
        _, _, injection_model = create_light_curve_model_from_args(
            args.kilonova_injection_model,
            args,
            sample_times,
            filters=filters,
            sample_over_Hubble=args.sample_over_Hubble,
        )
        data = create_light_curve_data(
            injection_parameters, args, light_curve_model=injection_model
        )
        print("Injection generated")

        if args.injection_outfile is not None:
            if filters is not None:
                if args.injection_detection_limit is None:
                    detection_limit = {x: np.inf for x in filters}
                else:
                    detection_limit = {
                        x: float(y)
                        for x, y in zip(
                            filters,
                            args.injection_detection_limit.split(","),
                        )
                    }
            else:
                detection_limit = {}
            data_out = np.empty((0, 6))
            for filt in data.keys():
                if filters:
                    if args.photometry_augmentation_filters:
                        filts = list(
                            set(
                                filters
                                + args.photometry_augmentation_filters.split(",")
                            )
                        )
                    else:
                        filts = filters
                    if filt not in filts:
                        continue
                for row in data[filt]:
                    mjd, mag, mag_unc = row
                    if not np.isfinite(mag_unc):
                        data_out = np.append(
                            data_out,
                            np.array([[mjd, 99.0, 99.0, filt, mag, 0.0]]),
                            axis=0,
                        )
                    else:
                        if filt in detection_limit:
                            data_out = np.append(
                                data_out,
                                np.array(
                                    [
                                        [
                                            mjd,
                                            mag,
                                            mag_unc,
                                            filt,
                                            detection_limit[filt],
                                            0.0,
                                        ]
                                    ]
                                ),
                                axis=0,
                            )
                        else:
                            data_out = np.append(
                                data_out,
                                np.array([[mjd, mag, mag_unc, filt, np.inf, 0.0]]),
                                axis=0,
                            )

            columns = ["jd", "mag", "mag_unc", "filter", "limmag", "programid"]
            lc = pd.DataFrame(data=data_out, columns=columns)
            lc.sort_values("jd", inplace=True)
            lc = lc.reset_index(drop=True)
            lc.to_csv(args.injection_outfile)

    else:
        # load the kilonova afterglow data
        try:
            data = loadEvent(args.data)

        except ValueError:
            with open(args.data) as f:
                data = json.load(f)
                for key in data.keys():
                    data[key] = np.array(data[key])

        if args.trigger_time is None:
            # load the minimum time as trigger time
            min_time = np.inf
            for key, array in data.items():
                min_time = np.minimum(min_time, np.min(array[:, 0]))
            trigger_time = min_time
            print(
                f"trigger_time is not provided, analysis will continue using a trigger time of {trigger_time}"
            )
        else:
            trigger_time = args.trigger_time

    if args.remove_nondetections:
        filters_to_check = list(data.keys())
        for filt in filters_to_check:
            idx = np.where(np.isfinite(data[filt][:, 2]))[0]
            data[filt] = data[filt][idx, :]
            if len(idx) == 0:
                del data[filt]

    # check for detections
    detection = False
    notallnan = False
    for filt in data.keys():
        idx = np.where(np.isfinite(data[filt][:, 2]))[0]
        if len(idx) > 0:
            detection = True
        idx = np.where(np.isfinite(data[filt][:, 1]))[0]
        if len(idx) > 0:
            notallnan = True
        if detection and notallnan:
            break
    if (not detection) or (not notallnan):
        raise ValueError("Need at least one detection to do fitting.")

    if type(args.error_budget) in [float, int]:
        error_budget = [args.error_budget]
    else:
        error_budget = [float(x) for x in args.error_budget.split(",")]
    if args.filters:
        if args.photometry_augmentation_filters:
            filters = list(
                set(
                    args.filters.split(",")
                    + args.photometry_augmentation_filters.split(",")
                )
            )
        else:
            filters = args.filters.split(",")

        values_to_indices = {v: i for i, v in enumerate(filters)}
        filters_to_analyze = sorted(
            list(set(filters).intersection(set(list(data.keys())))),
            key=lambda v: values_to_indices[v],
        )

        if len(error_budget) == 1:
            error_budget = dict(
                zip(filters_to_analyze, error_budget * len(filters_to_analyze))
            )
        elif len(args.filters.split(",")) == len(error_budget):
            error_budget = dict(zip(args.filters.split(","), error_budget))
        else:
            raise ValueError("error_budget must be the same length as filters")

    else:
        filters_to_analyze = list(data.keys())
        error_budget = dict(
            zip(filters_to_analyze, error_budget * len(filters_to_analyze))
        )

    print("Running with filters {0}".format(filters_to_analyze))
    model_names, models, light_curve_model = create_light_curve_model_from_args(
        args.model,
        args,
        sample_times,
        filters=filters_to_analyze,
        sample_over_Hubble=args.sample_over_Hubble,
    )

    # setup the prior
    priors = create_prior_from_args(model_names, args)

    # setup the likelihood
    if args.detection_limit:
        args.detection_limit = literal_eval(args.detection_limit)
    likelihood_kwargs = dict(
        light_curve_model=light_curve_model,
        filters=filters_to_analyze,
        light_curve_data=data,
        trigger_time=trigger_time,
        tmin=args.tmin,
        tmax=args.tmax,
        error_budget=error_budget,
        verbose=args.verbose,
        detection_limit=args.detection_limit,
    )

    likelihood = OpticalLightCurve(**likelihood_kwargs)
    if args.bilby_zero_likelihood_mode:
        likelihood = ZeroLikelihood(likelihood)

    # fetch the additional sampler kwargs
    sampler_kwargs = literal_eval(args.sampler_kwargs)
    print("Running with the following additional sampler_kwargs:")
    print(sampler_kwargs)

    # check if it is running with reactive sampler
    if args.reactive_sampling:
        if args.sampler != "ultranest":
            print("Reactive sampling is only available in ultranest")
        else:
            print("Running with reactive-sampling in ultranest")
            nlive = None
    else:
        nlive = args.nlive

    if args.skip_sampling:
        print("Sampling for 1 iteration and plotting checkpointed results.")
        if args.sampler == "pymultinest":
            sampler_kwargs["max_iter"] = 1
        elif args.sampler == "ultranest":
            sampler_kwargs["niter"] = 1
        elif args.sampler == "dynesty":
            sampler_kwargs["maxiter"] = 1

    result = bilby.run_sampler(
        likelihood,
        priors,
        sampler=args.sampler,
        outdir=args.outdir,
        label=args.label,
        nlive=nlive,
        seed=args.seed,
        soft_init=args.soft_init,
        queue_size=args.cpus,
        check_point_delta_t=3600,
        **sampler_kwargs,
    )

    # check if it is running under mpi
    try:
        from mpi4py import MPI

        rank = MPI.COMM_WORLD.Get_rank()
        if rank == 0:
            pass
        else:
            return

    except ImportError:
        pass

    result.save_posterior_samples()

    if args.injection:
        injlist_all = []
        for model_name in model_names:
            if model_name in ["Bu2019nsbh"]:
                injlist = [
                    "luminosity_distance",
                    "inclination_EM",
                    "log10_mej_dyn",
                    "log10_mej_wind",
                ]
            elif model_name in ["Bu2019lm"]:
                injlist = [
                    "luminosity_distance",
                    "inclination_EM",
                    "KNphi",
                    "log10_mej_dyn",
                    "log10_mej_wind",
                ]
            else:
                injlist = ["luminosity_distance"] + model_parameters_dict[model_name]

            injlist_all = list(set(injlist_all + injlist))

        constant_columns = []
        for column in result.posterior:
            if len(result.posterior[column].unique()) == 1:
                constant_columns.append(column)

        injlist_all = list(set(injlist_all) - set(constant_columns))
        injection = {
            key: injection_parameters[key]
            for key in injlist_all
            if key in injection_parameters
        }
        result.plot_corner(parameters=injection)
    else:
        result.plot_corner()

    if args.bestfit or args.plot:
        posterior_file = os.path.join(
            args.outdir, f"{args.label}_posterior_samples.dat"
        )

        ##########################
        # Fetch bestfit parameters
        ##########################
        posterior_samples = pd.read_csv(posterior_file, header=0, delimiter=" ")
        bestfit_idx = np.argmax(posterior_samples.log_likelihood.to_numpy())
        bestfit_params = posterior_samples.to_dict(orient="list")
        for key in bestfit_params.keys():
            bestfit_params[key] = bestfit_params[key][bestfit_idx]
        print(
            f"Best fit parameters: {str(bestfit_params)}\nBest fit index: {bestfit_idx}"
        )

        #########################
        # Generate the lightcurve
        #########################
        _, mag = light_curve_model.generate_lightcurve(sample_times, bestfit_params)
        for filt in mag.keys():
            if bestfit_params["luminosity_distance"] > 0:
                mag[filt] += 5.0 * np.log10(
                    bestfit_params["luminosity_distance"] * 1e6 / 10.0
                )
        mag["bestfit_sample_times"] = sample_times

        if "timeshift" in bestfit_params:
            mag["bestfit_sample_times"] = (
                mag["bestfit_sample_times"] + bestfit_params["timeshift"]
            )

        ######################
        # calculate the chi2 #
        ######################
        processed_data = dataProcess(
            data, filters_to_analyze, trigger_time, args.tmin, args.tmax
        )
        chi2 = 0.0
        dof = 0.0
        chi2_per_dof_dict = {}
        for filt in filters_to_analyze:
            # make best-fit lc interpolation
            sample_times = mag["bestfit_sample_times"]
            mag_used = mag[filt]
            interp = interp1d(sample_times, mag_used)
            # fetch data
            samples = copy.deepcopy(processed_data[filt])
            t, y, sigma_y = samples[:, 0], samples[:, 1], samples[:, 2]
            # shift t values by timeshift
            if "timeshift" in bestfit_params:
                t += bestfit_params["timeshift"]
            # only the detection data are needed
            finite_idx = np.where(np.isfinite(sigma_y))[0]
            if len(finite_idx) > 0:
                # fetch the erorr_budget
                if "em_syserr" in bestfit_params:
                    err = bestfit_params["em_syserr"]
                else:
                    err = error_budget[filt]
                t_det, y_det, sigma_y_det = (
                    t[finite_idx],
                    y[finite_idx],
                    sigma_y[finite_idx],
                )
                num = (y_det - interp(t_det)) ** 2
                den = sigma_y_det**2 + err**2
                chi2_per_filt = np.sum(num / den)
                # store the data
                chi2 += chi2_per_filt
                dof += len(finite_idx)
                chi2_per_dof_dict[filt] = chi2_per_filt / len(finite_idx)

        chi2_per_dof = chi2 / dof

    if args.bestfit:
        bestfit_to_write = bestfit_params.copy()
        bestfit_to_write["log_bayes_factor"] = result.log_bayes_factor
        bestfit_to_write["log_bayes_factor_err"] = result.log_evidence_err
        bestfit_to_write["Best fit index"] = int(bestfit_idx)
        bestfit_to_write["Magnitudes"] = {i: mag[i].tolist() for i in mag.keys()}
        bestfit_to_write["chi2_per_dof"] = chi2_per_dof
        bestfit_to_write["chi2_per_dof_per_filt"] = {
            i: chi2_per_dof_dict[i].tolist() for i in chi2_per_dof_dict.keys()
        }
        bestfit_file = os.path.join(args.outdir, f"{args.label}_bestfit_params.json")

        with open(bestfit_file, "w") as file:
            json.dump(bestfit_to_write, file, indent=4)

        print(f"Saved bestfit parameters and magnitudes to {bestfit_file}")

    if args.plot:
        import matplotlib.pyplot as plt
        from matplotlib.pyplot import cm

        if len(models) > 1:
            _, mag_all = light_curve_model.generate_lightcurve(
                sample_times, bestfit_params, return_all=True
            )

            for ii in range(len(mag_all)):
                for filt in mag_all[ii].keys():
                    if bestfit_params["luminosity_distance"] > 0:
                        mag_all[ii][filt] += 5.0 * np.log10(
                            bestfit_params["luminosity_distance"] * 1e6 / 10.0
                        )
            model_colors = cm.Spectral(np.linspace(0, 1, len(models)))[::-1]

        filters_plot = []
        for filt in filters_to_analyze:
            if filt not in data:
                continue
            samples = data[filt]
            t, y, sigma_y = samples[:, 0], samples[:, 1], samples[:, 2]
            idx = np.where(~np.isnan(y))[0]
            t, y, sigma_y = t[idx], y[idx], sigma_y[idx]
            if len(t) == 0:
                continue
            filters_plot.append(filt)

        colors = cm.Spectral(np.linspace(0, 1, len(filters_plot)))[::-1]

        plotName = os.path.join(args.outdir, f"{args.label}_lightcurves.png")
        plt.figure(figsize=(20, 16))
        color2 = "coral"

        cnt = 0
        for filt, color in zip(filters_plot, colors):
            cnt = cnt + 1
            if cnt == 1:
                ax1 = plt.subplot(len(filters_plot), 1, cnt)
            else:
                ax2 = plt.subplot(len(filters_plot), 1, cnt, sharex=ax1, sharey=ax1)

            samples = data[filt]
            t, y, sigma_y = samples[:, 0], samples[:, 1], samples[:, 2]
            t -= trigger_time
            idx = np.where(~np.isnan(y))[0]
            t, y, sigma_y = t[idx], y[idx], sigma_y[idx]

            idx = np.where(np.isfinite(sigma_y))[0]
            plt.errorbar(
                t[idx],
                y[idx],
                sigma_y[idx],
                fmt="o",
                color="k",
                markersize=16,
            )  # or color=color

            idx = np.where(~np.isfinite(sigma_y))[0]
            plt.errorbar(
                t[idx], y[idx], sigma_y[idx], fmt="v", color="k", markersize=16
            )  # or color=color

            mag_plot = getFilteredMag(mag, filt)

            plt.plot(
                mag["bestfit_sample_times"],
                mag_plot,
                color=color2,
                linewidth=3,
                linestyle="--",
            )

            if len(models) > 1:
                plt.fill_between(
                    mag["bestfit_sample_times"],
                    mag_plot + error_budget[filt],
                    mag_plot - error_budget[filt],
                    facecolor=color2,
                    alpha=0.2,
                    label="Combined",
                )
            else:
                plt.fill_between(
                    mag["bestfit_sample_times"],
                    mag_plot + error_budget[filt],
                    mag_plot - error_budget[filt],
                    facecolor=color2,
                    alpha=0.2,
                )

            if len(models) > 1:
                for ii in range(len(mag_all)):
                    mag_plot = getFilteredMag(mag_all[ii], filt)
                    plt.plot(
                        mag["bestfit_sample_times"],
                        mag_plot,
                        color=color2,
                        linewidth=3,
                        linestyle="--",
                    )
                    plt.fill_between(
                        mag["bestfit_sample_times"],
                        mag_plot + error_budget[filt],
                        mag_plot - error_budget[filt],
                        facecolor=model_colors[ii],
                        alpha=0.2,
                        label=models[ii].model,
                    )

            plt.ylabel("%s" % filt, fontsize=48, rotation=0, labelpad=40)

            plt.xlim([float(x) for x in args.xlim.split(",")])
            plt.ylim([float(x) for x in args.ylim.split(",")])
            plt.grid()

            if cnt == 1:
                ax1.set_yticks([26, 22, 18, 14])
                plt.setp(ax1.get_xticklabels(), visible=False)
                if len(models) > 1:
                    plt.legend(
                        loc="upper right",
                        prop={"size": 18},
                        numpoints=1,
                        shadow=True,
                        fancybox=True,
                    )
            elif not cnt == len(filters_plot):
                plt.setp(ax2.get_xticklabels(), visible=False)
            plt.xticks(fontsize=36)
            plt.yticks(fontsize=36)

        ax1.set_zorder(1)
        plt.xlabel("Time [days]", fontsize=48)
        plt.tight_layout()
        plt.savefig(plotName)
        plt.close()


def main(args=None):
    if args is None:
        parser = get_parser()
        args = parser.parse_args()
        if args.config is not None:
            yaml_dict = yaml.safe_load(Path(args.config).read_text())
            for analysis_set in yaml_dict.keys():
                params = yaml_dict[analysis_set]
                for key, value in params.items():
                    key = key.replace("-", "_")
                    if key not in args:
                        print(f"{key} not a known argument... please remove")
                        exit()
                    setattr(args, key, value)
                analysis(args)
        else:
            analysis(args)
    else:
        analysis(args)
