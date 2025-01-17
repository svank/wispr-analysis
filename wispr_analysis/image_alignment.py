from collections import defaultdict
from itertools import chain, repeat
from math import ceil, floor
import os
import pickle
import shutil
import warnings


from astropy.io import fits
from astropy.wcs import DistortionLookupTable, WCS
from IPython.core.display import HTML
from IPython.display import display
import matplotlib.pyplot as plt
from numba import njit
import numpy as np
import scipy.ndimage
import scipy.optimize
import scipy.stats
from tqdm.auto import tqdm
from tqdm.contrib.concurrent import process_map

from . import star_tools, utils


def make_cutout(x, y, data, cutout_size, normalize=True):
    """
    Cuts a section from a data array centered on a coordinate and normalizes it.
    
    Raises an error if the cutout extends beyond the data bounds.
    
    Parameters
    ----------
    x, y : float
        Floating-point array indices around which to center the cutout
    data : array
        The data out of which to take the cutout
    cutout_size : int
        The size of the square cutout, in pixels
    normalize : boolean
        Whether to normalize the data in the cutout
    
    Returns
    -------
    cutout : array
        The cutout
    cutout_start_x, cutout_start_y : int
        The array indices in the full array of the first row/column in the
        cutout
    """
    cutout_size = int(round(cutout_size))
    
    cutout_start_x = int(round(x)) - cutout_size//2
    cutout_start_y = int(round(y)) - cutout_size//2
    
    assert 0 < cutout_start_y < data.shape[0] - cutout_size + 1
    assert 0 < cutout_start_x < data.shape[1] - cutout_size + 1
    
    cutout = data[
            cutout_start_y:cutout_start_y + cutout_size,
            cutout_start_x:cutout_start_x + cutout_size]
    
    if normalize:
        cutout = cutout - np.min(cutout)
        with np.errstate(invalid='raise'):
            cutout = cutout / np.max(cutout)
    
    return cutout, cutout_start_x, cutout_start_y


MIN_SIGMA = 0.05
MAX_SIGMA = 1.5
def fit_star(x, y, data, all_stars_x, all_stars_y, cutout_size=9,
        ret_more=False, ret_star=False, binning=2, start_at_max=True,
        normalize_cutout=True):
    bin_factor = 2 / binning
    cutout_size = int(round(cutout_size * bin_factor))
    if cutout_size % 2 != 1:
        cutout_size += 1
    
    try:
        cutout, cutout_start_x, cutout_start_y = make_cutout(
                x, y, data, cutout_size, normalize_cutout)
    except FloatingPointError:
        err = ["Invalid values encountered"]
        if ret_star:
            return None, None, None, err
        if not ret_more:
            return np.nan, np.nan, np.nan, np.nan, np.nan, err
    
    cutout = cutout.astype(float)
    cutout_size = cutout.shape[0]
    
    err = []
    if np.any(np.isnan(cutout)):
        err.append("NaNs in cutout")
        if ret_star:
            return None, None, None, err
        if not ret_more:
            return x, y, np.nan, np.nan, np.nan, err
    
    if all_stars_x is None:
        all_stars_x = np.array([x])
    else:
        all_stars_x = np.asarray(all_stars_x)
    if all_stars_y is None:
        all_stars_y = np.array([y])
    else:
        all_stars_y = np.asarray(all_stars_y)
    
    n_in_cutout = np.sum(
        (all_stars_x > cutout_start_x - .5)
        * (all_stars_x < cutout_start_x + cutout_size - .5)
        * (all_stars_y > cutout_start_y - .5)
        * (all_stars_y < cutout_start_y + cutout_size - .5))
    
    if n_in_cutout > 1:
        err.append("Crowded frame")
        if ret_star:
            return None, None, None, err
        if not ret_more:
            return x, y, np.nan, np.nan, np.nan, err
    
    if start_at_max:
        i_max = np.argmax(cutout)
        y_start, x_start = np.unravel_index(i_max, cutout.shape)
    else:
        x_start = x - cutout_start_x
        y_start = y - cutout_start_y
    
    with warnings.catch_warnings():
        warnings.filterwarnings(action='error')
        try:
            # We'll follow astropy's example and apply bounds ourself with the
            # lm fitter, which is 10x faster than using the bounds-aware fitter
            # without any real change in outputs.
            bounds=np.array([
                (0,             # amplitude
                 -1,            # x0
                 -1,            # y0
                 -np.inf,       # x_std
                 -np.inf,       # y_std
                 -2*np.pi,      # theta
                 -np.inf,       # intercept
                 -np.inf,       # x slope
                 -np.inf),      # y slope
                (np.inf,        # amplitude
                 cutout_size,   # x0
                 cutout_size,   # y0
                 np.inf,        # x_std
                 np.inf,        # y_std
                 2*np.pi,       # theta
                 np.inf,        # intercept
                 np.inf,        # x slope
                 np.inf),       # y slope
                ])
            x0 = [cutout.max(),      # amplitude
                  x_start,           # x0
                  y_start,           # y0
                  bin_factor,        # x_std
                  bin_factor,        # y_std
                  0,                 # theta
                  np.median(cutout), # intercept
                  0,                 # x slope
                  0,                 # y slope
                 ]
            res = scipy.optimize.least_squares(
                    model_error,
                    x0,
                    args=(cutout, bounds),
                    method='lm'
                )
            A, xc, yc, xstd, ystd, theta, intercept, slope_x, slope_y = res.x
        except RuntimeWarning:
            err.append("No solution found")
            if ret_more:
                return None, cutout, err, cutout_start_x, cutout_start_y
            elif ret_star:
                return None, None, None, err
            else:
                return np.nan, np.nan, np.nan, np.nan, np.nan, err
    
    max_std = MAX_SIGMA * bin_factor
    min_std = MIN_SIGMA * bin_factor
    
    if A < 0.5 * (np.max(cutout) - intercept):
        err.append("No peak found")
    if xstd > max_std or ystd > max_std:
        err.append("Fit too wide")
    if xstd < min_std or ystd < min_std:
        err.append("Fit too narrow")
    if (not (0 < xc < cutout_size - 1)
            or not (0 < yc < cutout_size - 1)):
        err.append("Fitted peak too close to edge")
    
    if ret_more:
        return res, cutout, err, cutout_start_x, cutout_start_y
    if ret_star:
        star = model_fcn(
                (A, xc, yc, xstd, ystd, theta, 0, 0, 0),
                cutout)
        return star, cutout_start_x, cutout_start_y, err
    return (xc + cutout_start_x,
            yc + cutout_start_y,
            xstd,
            ystd,
            theta,
            err)


@njit
def model_fcn(params, cutout):
    x = np.empty(cutout.shape, dtype=np.int64)
    y = np.empty(cutout.shape, dtype=np.int64)
    for i in range(cutout.shape[0]):
        for j in range(cutout.shape[1]):
            x[i, j] = j
            y[i, j] = i
    
    A, xc, yc, xstd, ystd, theta, intercept, slope_x, slope_y = params
    
    a = np.cos(theta)**2 / (2 * xstd**2) + np.sin(theta)**2 / (2 * ystd**2)
    b = np.sin(2*theta)  / (2 * xstd**2) - np.sin(2*theta)  / (2 * ystd**2)
    c = np.sin(theta)**2 / (2 * xstd**2) + np.cos(theta)**2 / (2 * ystd**2)
    
    model = (
        A * np.exp(
            - a * (x-xc)**2
            - b * (x-xc) * (y-yc)
            - c * (y-yc)**2
        )
        + intercept + slope_x * x + slope_y * y
    )
    
    return model


@njit
def model_error(params, cutout, bounds):
    for i in range(len(params)):
        if params[i] < bounds[0][i]:
            params[i] = bounds[0][i]
        if params[i] > bounds[1][i]:
            params[i] = bounds[1][i]
    model = model_fcn(params, cutout)
    return (model - cutout).flatten()

DIM_CUTOFF = 8
BRIGHT_CUTOFF = 2

def prep_frame_for_star_finding(fname, dim_cutoff=DIM_CUTOFF,
        bright_cutoff=BRIGHT_CUTOFF, corrector=None):
    with utils.ignore_fits_warnings(), fits.open(fname) as hdul:
        data = hdul[0].data
        hdr = hdul[0].header
        w = WCS(hdr, hdul, key='A')
    
    if corrector is not None:
        data = corrector(data)
    
    if data.shape not in ((1024, 960), (2048, 1920)):
        raise ValueError(
                f"Strange image shape {data.shape} in {fname}---skipping")
    
    if hdr['nbin1'] != hdr['nbin2']:
        raise ValueError(f"There's some weird binning going on in {fname}")
    binning = hdr['nbin1']
    bin_factor = 2 / binning

    trim = (40, 20, 20, 20)
    trim = [int(round(t * bin_factor)) for t in trim]
    
    (stars_x, stars_y, stars_vmag, stars_ra, stars_dec, all_stars_x,
            all_stars_y) = star_tools.find_expected_stars_in_frame(
                    (hdr, w), trim=trim, dim_cutoff=dim_cutoff,
                    bright_cutoff=bright_cutoff)
    
    return (stars_x + trim[0], stars_y + trim[2], stars_vmag, stars_ra, stars_dec,
            all_stars_x, all_stars_y, data, binning)

def find_stars_in_frame(data):
    fname, start_at_max, include_shapes, corrector = data
    t = utils.to_timestamp(fname)
    try:
        (stars_x, stars_y, stars_vmag, stars_ra, stars_dec,
                all_stars_x, all_stars_y, data,
                binning) = prep_frame_for_star_finding(
                    fname, corrector=corrector)
    except ValueError as e:
        print(e)
        return [], [], [], {}, {}

    good = []
    crowded_out = []
    bad = []
    codes = {}
    
    mapping = {}
    
    for x, y, ra, dec, vmag in zip(
            stars_x, stars_y, stars_ra, stars_dec, stars_vmag):
        fx, fy, fxstd, fystd, theta, err = fit_star(
                x, y, data, all_stars_x, all_stars_y,
                ret_more=False, binning=binning,
                start_at_max=start_at_max)

        results = (fx, fy)
        if include_shapes:
            results += (fxstd, fystd, theta)
        if len(err) == 0:
            good.append(results)
            mapping[(ra, dec)] = (*results, t, vmag)
        elif 'Crowded frame' in err:
            crowded_out.append(results)
        else:
            bad.append(results)
        for e in err:
            codes[e] = codes.get(e, 0) + 1
    return good, bad, crowded_out, codes, mapping


def find_all_stars(ifiles, ret_all=False, start_at_max=True,
        include_shapes=False, corrector=None):
    res = process_map(
            find_stars_in_frame,
            zip(ifiles, repeat(start_at_max), repeat(include_shapes),
                repeat(corrector)),
            total=len(ifiles))

    good = []
    crowded_out = []
    bad = []
    codes = {}
    mapping = defaultdict(list)
    mapping_by_frame = {}

    for fname, (good_in_frame, bad_in_frame, crowded_in_frame,
            codes_in_frame, mapping_in_frame) in zip(ifiles, res):
        t = utils.to_timestamp(fname)
        good.extend(good_in_frame)
        bad.extend(bad_in_frame)
        crowded_out.extend(crowded_in_frame)
        for code, count in codes_in_frame.items():
            codes[code] = codes.get(code, 0) + count
        
        mapping_by_frame[t] = []
        for celest_coords, star_data in mapping_in_frame.items():
            mapping[celest_coords].append(star_data)
            mapping_by_frame[t].append(
                    (*celest_coords, *star_data[:2], star_data[3]))

    # The `reshape` calls handle the case that the input list is empty
    n = 5 if include_shapes else 2
    good_x, good_y = np.array(good).T.reshape((n, -1))[:2]
    crowded_out_x, crowded_out_y = np.array(crowded_out).T.reshape((n, -1))[:2]
    bad_x, bad_y = np.array(bad).T.reshape((n, -1))[:2]
    
    # Change the defaultdict to just a dict
    mapping = {k:v for k, v in mapping.items()}
    
    if ret_all:
        return (mapping, mapping_by_frame, good_x, good_y, crowded_out_x,
                crowded_out_y, bad_x, bad_y, codes)
    return mapping, mapping_by_frame


def do_iteration_with_crpix(pts1, pts2, w1, w2, angle_start, dra_start,
        ddec_start, dx_start, dy_start):
    def f(args):
        angle, dra, ddec, dx, dy = args
        rot = np.array([[np.cos(angle), -np.sin(angle)],
                        [np.sin(angle), np.cos(angle)]])
        w22 = w2.deepcopy()
        w22.wcs.crval = w22.wcs.crval + np.array([dra, ddec])
        w22.wcs.crpix = w22.wcs.crpix + np.array([dx, dy])
        w22.wcs.pc = rot @ w22.wcs.pc
        pts22 = np.array(w1.world_to_pixel(w22.pixel_to_world(
            pts2[:, 0], pts2[:, 1]))).T
        ex = pts22[:, 0] - pts1[:, 0]
        ey = pts22[:, 1] - pts1[:, 1]
        err = np.sqrt(ex**2 + ey**2)
        return err
    res = scipy.optimize.least_squares(
            f,
            [angle_start, dra_start, ddec_start, dx_start, dy_start],
            bounds=[[-np.pi, -np.inf, -np.inf, -np.inf, -np.inf],
                    [np.pi, np.inf, np.inf, np.inf, np.inf]])
    return res, res.x


def do_iteration_no_crpix(ras, decs, xs_true, ys_true, w):
    def f(args):
        angle, dra, ddec = args
        rot = np.array([[np.cos(angle), -np.sin(angle)],
                        [np.sin(angle), np.cos(angle)]])
        w2 = w.deepcopy()
        w2.wcs.crval = w2.wcs.crval + np.array([dra, ddec])
        w2.wcs.pc = rot @ w2.wcs.pc
        xs, ys = np.array(w2.all_world2pix(ras, decs, 0))
        ex = xs_true - xs
        ey = ys_true - ys
        err = np.sqrt(ex**2 + ey**2)
        # err[err > 1] = 1
        return err
    res = scipy.optimize.least_squares(
            f,
            [0, 0, 0],
            bounds=[[-np.pi, -np.inf, -np.inf],
                    [np.pi, np.inf, np.inf]],
                                      # loss='cauchy'
                                      )
    return res, *res.x, 0, 0


do_iteration = do_iteration_no_crpix


def iteratively_align_one_file(data):
    fname, series_data, out_dir, write_file = data
    with utils.ignore_fits_warnings():
        d, h = fits.getdata(fname, header=True)
        w = WCS(h, key='A')
    t = utils.to_timestamp(fname)
    
    # Check up here, especially in case the frame has *zero* identified stars
    # (i.e. a very bad frame)
    if len(series_data) < 50:
        print(f"Only {len(series_data)} stars found in {fname}---skipping")
        return np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan
    
    ras, decs, xs, ys, _ = zip(*series_data)
    xs = np.array(xs)
    ys = np.array(ys)
    ras = np.array(ras)
    decs = np.array(decs)
    
    xs_comp, ys_comp = np.array(w.all_world2pix(ras, decs, 0))
    dx = xs_comp - xs
    dy = ys_comp - ys
    dr = np.sqrt(dx**2 + dy**2)
    outlier_cutoff = dr.mean() + 2 * dr.std()
    inliers = dr < outlier_cutoff
    
    xs = xs[inliers]
    ys = ys[inliers]
    ras = ras[inliers]
    decs = decs[inliers]
    
    # Check down here after outlier removal
    if len(series_data) < 50:
        print(f"Only {len(series_data)} stars found in {fname}"
               "after outlier removal---skipping")
        return np.nan, np.nan, np.nan, np.nan, np.nan, np.nan, np.nan

    res, angle, dra, ddec, dx, dy = do_iteration(ras, decs, xs, ys, w)
    
    if write_file:
        update_file_with_offset(angle, dra, ddec, dx, dy,
                os.path.join(out_dir, os.path.basename(fname)),
                h, d, w)
                
    rmse = np.sqrt(np.mean(np.sqrt(res.fun)))
    return angle, dx, dy, dra, ddec, t, rmse


def update_file_with_offset(angle, dra, ddec, dx, dy, outfile,
        header=None, data=None, wcs=None, infile=None):
    with utils.ignore_fits_warnings():
        if infile is not None:
            data, header = fits.getdata(infile, header=True)
        wcs_hp = WCS(header, key=' ')
        wcs_ra = WCS(header, key='A')
    
    # Update the RA/Dec coordinates directly
    header_matrix = np.array([[np.cos(angle), -np.sin(angle)],
                              [np.sin(angle), np.cos(angle)]]) @ wcs_ra.wcs.pc
    header['PC1_1A'] = header_matrix[0, 0]
    header['PC1_2A'] = header_matrix[0, 1]
    header['PC2_1A'] = header_matrix[1, 0]
    header['PC2_2A'] = header_matrix[1, 1]
    header['CRVAL1A'] = header['CRVAL1A'] + dra
    header['CRVAL2A'] = header['CRVAL2A'] + ddec
    header['CRPIX1A'] = header['CRPIX1A'] + dx
    header['CRPIX2A'] = header['CRPIX2A'] + dy
    
    # Apply the same rotation to the HP coords
    header_matrix = np.array([[np.cos(angle), -np.sin(angle)],
                              [np.sin(angle), np.cos(angle)]]) @ wcs_hp.wcs.pc
    header['PC1_1 '] = header_matrix[0, 0]
    header['PC1_2 '] = header_matrix[0, 1]
    header['PC2_1 '] = header_matrix[1, 0]
    header['PC2_2 '] = header_matrix[1, 1]
    
    # Convert the new RA/Dec reference coord to its corresponding HP coord,
    # using the original reference frames.
    ref_px = wcs_ra.all_world2pix(header['CRVAL1A'], header['CRVAL2A'], 0)
    ref_val = wcs_hp.all_pix2world(*ref_px, 0)
    
    # Set the corresponding HP coord as the HP reference coord
    # Convert values to float b/c Headers don't like assignment of Numpy
    # elements, apparently.
    header['CRVAL1 '] = float(ref_val[0])
    header['CRVAL2 '] = float(ref_val[1])
    header['CRPIX1 '] = header['CRPIX1 '] + dx
    header['CRPIX2 '] = header['CRPIX2 '] + dy
    
    with utils.ignore_fits_warnings():
        fits.writeto(outfile, data, header=header, overwrite=True)


def iteratively_align_files(file_list, out_dir, series_by_frame,
        smooth_offsets=True):
    os.makedirs(out_dir, exist_ok=True)
    
    data = process_map(iteratively_align_one_file, zip(
                           file_list,
                           (series_by_frame[utils.to_timestamp(fname)]
                               for fname in file_list),
                           repeat(out_dir),
                           repeat(not smooth_offsets)),
                       total=len(file_list))
    
    data = np.array(data)
    angle_ts = data[:, 0]
    rpix_ts = data[:, 1:3]
    rval_ts = data[:, 3:5]
    t_vals = data[:, 5]
    rmses = data[:, 6]
    
    if smooth_offsets:
        angle_ts = smooth_curve(t_vals, angle_ts,
                sig=smooth_offsets, n_sig=3, outlier_sig=2)
        rpix_ts[:, 0] = smooth_curve(t_vals, rpix_ts[:, 0],
                sig=smooth_offsets, n_sig=3, outlier_sig=2)
        rpix_ts[:, 1] = smooth_curve(t_vals, rpix_ts[:, 1],
                sig=smooth_offsets, n_sig=3, outlier_sig=2)
        rval_ts[:, 0] = smooth_curve(t_vals, rval_ts[:, 0],
                sig=smooth_offsets, n_sig=3, outlier_sig=2)
        rval_ts[:, 1] = smooth_curve(t_vals, rval_ts[:, 1],
                sig=smooth_offsets, n_sig=3, outlier_sig=2)
        
        print("Writing out files...")
        for fname, angle, rpix, rval in zip(
                tqdm(file_list), angle_ts, rpix_ts, rval_ts):
            if np.isnan(angle):
                continue
            update_file_with_offset(angle, rval[0], rval[1], rpix[0], rpix[1],
                    os.path.join(out_dir, os.path.basename(fname)),
                    infile=fname)
    
    return angle_ts, rval_ts, rpix_ts, t_vals, rmses


def smooth_curve(x, y, sig=3600*6.5, n_sig=3, outlier_sig=2):
    """
    Applies a Gaussian filter to an unevenly-spaced 1D signal.
    
    The Gaussian is evaluated with resepct to x-axis values, rather than
    array coordinates (or pixel indicies, etc.)
    
    NaN values are ignored in the kernel integration, and NaNs in the input
    will be replaced with a smoothed value.
    
    Parameters
    ----------
    x, y : numpy arrays
        x and y values of the signal
    sig
        Width of the standard deviation of the Gaussian, in the same units as
        ``x``
    n_sig
        The Gaussian kernel is integrated out to this many standard deviations
    outlier_sig
        Before integrating the kernel, the window from -n_sig to +n_sig is
        checked for outliers. Any point deviating from the window's mean by at
        least ``outlier_sig`` times the window standard deviation is ignored.
    """
    output_array = np.zeros_like(y, dtype=float)
    not_nan = np.isfinite(y)
    for i in range(len(y)):
        f = np.abs(x - x[i]) <= n_sig * sig
        f *= not_nan
        xs = x[f]
        ys = y[f]
        window_std = np.std(ys)
        # Skip outlier rejection if there's no variation within the window
        if window_std > 0:
            f = np.abs(ys - np.mean(ys)) <= outlier_sig * window_std
            xs = xs[f]
            ys = ys[f]
        weight = np.exp(-(x[i] - xs)**2 / sig**2)
        output_array[i] = np.sum(weight * ys) / weight.sum()
    return output_array


def iteratively_perturb_projections(file_list, out_dir, series_by_frame,
                                    also_shear=False, n_extra_params=0,
                                    do_print=True, weights=None):
    wcses = []
    all_ras = []
    all_decs = []
    all_xs_true = []
    all_ys_true = []
    
    if weights is None:
        weights = np.ones(len(file_list))
    
    if do_print:
        print("Reading files...")
    with utils.ignore_fits_warnings():
        for fname in file_list:
            t = utils.to_timestamp(fname)
            series_data = series_by_frame[t]
            wcs = WCS(fits.getheader(fname), key='A')
            
            # Check up here, especially in case the frame has *zero* identified
            # stars (i.e. a very bad frame)
            if len(series_data) < 50:
                print(f"Only {len(series_data)} stars found in "
                      f"{fname}---skipping")
                continue

            if len(series_data[0]) == 4:
                ras, decs, xs, ys = zip(*series_data)
            else:
                ras, decs, xs, ys, _ = zip(*series_data)
            ras = np.array(ras)
            decs = np.array(decs)
            xs = np.array(xs)
            ys = np.array(ys)

            xs_comp, ys_comp = np.array(wcs.all_world2pix(ras, decs, 0))
            dx = xs_comp - xs
            dy = ys_comp - ys
            dr = np.sqrt(dx**2 + dy**2)
            outlier_cutoff = dr.mean() + 2 * dr.std()
            inliers = dr < outlier_cutoff

            xs = xs[inliers]
            ys = ys[inliers]
            ras = ras[inliers]
            decs = decs[inliers]

            # Check down here after outlier removal
            if len(series_data) < 50:
                print(f"Only {len(series_data)} stars found in {fname} "
                       "after outlier removal---skipping")
                continue
            
            all_ras.append(ras)
            all_decs.append(decs)
            all_xs_true.append(xs)
            all_ys_true.append(ys)
            
            wcses.append(wcs)
    
    if do_print:
        print("Doing iteration...")
    pv_orig = wcses[0].wcs.get_pv()
    def f(pv_perts):
        if also_shear:
            shear_x = pv_perts[0]
            shear_y = pv_perts[1]
            pv_perts = pv_perts[2:]
            shear = (np.array([[1, shear_x], [0, 1]])
                     @ np.array([[1, 0], [shear_y, 1]]))
        pv = wcses[0].wcs.get_pv()
        
        for i, pv_pert in enumerate(pv_perts):
            for j, elem in enumerate(pv_orig):
                if elem[0] == 2 and elem[1] == i:
                    elem = (elem[0], elem[1], pv_pert * elem[2])
                    pv[j] = elem
                    break
            else:
                pv.append((2, i, pv_pert))
        
        err = []
        for w, ras, decs, xs, ys, weight in zip(
                wcses, all_ras, all_decs, all_xs_true, all_ys_true, weights):
            if also_shear:
                w = w.deepcopy()
                w.wcs.pc = shear @ w.wcs.pc
            w.wcs.set_pv(pv)
            
            xs_comp, ys_comp = np.array(w.all_world2pix(ras, decs, 0))
            ex = xs - xs_comp
            ey = ys - ys_comp
            err.append(np.sqrt(ex**2 + ey**2) * np.sqrt(weight))
        
        return np.concatenate(err)
    
    n_pvs = len(
        [e for e in wcses[0].wcs.get_pv() if e[0] == 2])
    res = scipy.optimize.least_squares(
            f, ([0, 0] if also_shear else []) + ([1] * n_pvs) + ([0] * n_extra_params))
    
    if also_shear:
        shear_x = res.x[0]
        shear_y = res.x[1]
        res.x = res.x[2:]
        shear = (np.array([[1, shear_x], [0, 1]])
                 @ np.array([[1, 0], [shear_y, 1]]))
    else:
        shear_x, shear_y = 0, 0
        shear = np.array([[1, 0], [0, 1]])
    
    with utils.ignore_fits_warnings():
        header = fits.getheader(file_list[0])
    
    update_dict = {}
    for i, pert in enumerate(res.x):
        for wcs_key in ('A', ' '):
            key = f'PV2_{i}' + wcs_key
            if key in header:
                update_dict[key] = pert * header[key]
            else:
                update_dict[key] = pert
    
    if out_dir is not None:
        if do_print:
            print("Writing out updated files...")
        os.makedirs(out_dir, exist_ok=True)
        for fname in file_list:
            update_file_with_projection(
                    fname, update_dict, out_dir,
                    also_shear=also_shear, shear=shear)
    return update_dict, pv_orig, shear_x, shear_y


def update_file_with_projection(input_fname, update_dict, out_dir,
        also_shear=False, shear=None):
    with utils.ignore_fits_warnings():
        with fits.open(input_fname) as hdul:
            hdul[0].header.update(update_dict)
            if also_shear:
                for wcs_key in ('A', ' '):
                    w = WCS(hdul[0].header, key=wcs_key)
                    w.wcs.pc = shear @ w.wcs.pc
                    update = w.to_header(key=wcs_key)
                    for k in update:
                        if k.startswith("PC"):
                            hdul[0].header[k] = update[k]
            hdul.writeto(os.path.join(out_dir,
                         os.path.basename(input_fname)),
                         overwrite=True)


def calc_binned_err_components(px_x, px_y, err_x, err_y, ret_coords=False):
    if np.any(px_x > 960) or np.any(px_y > 1024):
        raise ValueError("Unexpected binning for this image")
    
    berr_x, r, c, _ = scipy.stats.binned_statistic_2d(
        px_y, px_x, err_x, 'median', (1024//10, 960//10),
        expand_binnumbers=True,
        range=((0, 1024), (0, 960)))
    
    berr_y, _, _, _ = scipy.stats.binned_statistic_2d(
        px_y, px_x, err_y, 'median', (1024//10, 960//10),
        expand_binnumbers=True,
        range=((0, 1024), (0, 960)))
    
    if ret_coords:
        r = (r[1:] + r[:-1]) / 2
        c = (c[1:] + c[:-1]) / 2
        return berr_x, berr_y, c, r
    
    return berr_x, berr_y


def filter_distortion_table(data, blur_sigma=4, med_filter_size=3):
    """
    Returns a filtered copy of a distortion map table.
    
    Any rows/columns at the edges that are all NaNs will be removed and
    replaced with a copy of the closest non-removed edge at the end of
    processing.
    
    Any NaN values that don't form a complete edge row/column will be replaced
    with the median of all surrounding non-NaN pixels.
    
    Then median filtering is performed across the whole map to remove outliers,
    and Gaussian filtering is applied to accept only slowly-varying
    distortions.
    
    Parameters
    ----------
    data
        The distortion map to be filtered
    blur_sigma : float
        The number of pixels constituting one standard deviation of the
        Gaussian kernel. Set to 0 to disable Gaussian blurring.
    med_filter_size : int
        The size of the local neighborhood to consider for median filtering.
        Set to 0 to disable median filtering.
    """
    data = data.copy()
    
    # Trim empty (all-nan) rows and columns
    trimmed = []
    i = 0
    while np.all(np.isnan(data[0])):
        i += 1
        data = data[1:]
    trimmed.append(i)

    i = 0
    while np.all(np.isnan(data[-1])):
        i += 1
        data = data[:-1]
    trimmed.append(i)

    i = 0
    while np.all(np.isnan(data[:, 0])):
        i += 1
        data = data[:, 1:]
    trimmed.append(i)

    i = 0
    while np.all(np.isnan(data[:, -1])):
        i += 1
        data = data[:, :-1]
    trimmed.append(i)
    
    # Replace interior nan values with the median of the surrounding values.
    # We're filling in from neighboring pixels, so if there are any nan pixels
    # fully surrounded by nan pixels, we need to iterate a few times.
    while np.any(np.isnan(data)):
        nans = np.nonzero(np.isnan(data))
        replacements = np.zeros_like(data)
        with warnings.catch_warnings():
            warnings.filterwarnings(action='ignore', message='All-NaN slice')
            for r, c in zip(*nans):
                r1, r2 = r-1, r+2
                c1, c2 = c-1, c+2
                r1, r2 = max(r1, 0), min(r2, data.shape[0])
                c1, c2 = max(c1, 0), min(c2, data.shape[1])

                replacements[r, c] = np.nanmedian(data[r1:r2, c1:c2])
        data[nans] = replacements[nans]
    
    # Median-filter the whole image
    if med_filter_size:
        data = scipy.ndimage.median_filter(data, size=med_filter_size, mode='reflect')
    
    # Gaussian-blur the whole image
    if blur_sigma > 0:
        data = scipy.ndimage.gaussian_filter(data, sigma=blur_sigma)
    
    # Replicate the edge rows/columns to replace those we trimmed earlier
    data = np.pad(data, [trimmed[0:2], trimmed[2:]], mode='edge')
    
    return data


def add_distortion_table(fname, outname, err_x, err_y, err_px, err_py):
    """
    Adds two distortion maps to a FITS file, for x and y distortion.
    
    Parameters
    ----------
    fname
        The path to the input FITS file, to which distortions should be added
    outname
        The path to which the updated FITS file should be saved. If ``None``,
        the updated ``HDUList`` is returned instead.
    err_x, err_y
        The distortion values, given in the sense of "the coordinate computed
        for a pixel is offset by this much from its true location". The
        negative of these values will be stored as the distortion map, and that
        is the amount by which pixel coordinates will be shifted before being
        converted to world coordinates.
    err_px, err_py
        The x or y coordinate associated with each pixel in the provided
        distortion maps
    """
    dx = DistortionLookupTable(-err_x.astype(np.float32),
                               (1, 1),
                               (err_px[0] + 1, err_py[0] + 1),
                               ((err_px[1] - err_px[0]),
                                   (err_py[1] - err_py[0])))
    dy = DistortionLookupTable(-err_y.astype(np.float32),
                               (1, 1),
                               (err_px[0] + 1, err_py[0] + 1),
                               ((err_px[1] - err_px[0]),
                                   (err_py[1] - err_py[0])))
    with utils.ignore_fits_warnings():
        data, header = fits.getdata(fname, header=True)
        wcs = WCS(header, key='A')
    wcs.cpdis1 = dx
    wcs.cpdis2 = dy
    hdul = wcs.to_fits()
    
    for key in ('extend', 'cpdis1', 'cpdis2',
                'dp1.EXTVER', 'dp1.NAXES', 'dp1.AXIS.1', 'dp1.AXIS.2',
                'dp2.EXTVER', 'dp2.NAXES', 'dp2.AXIS.1', 'dp2.AXIS.2'):
        header[key] = hdul[0].header[key]
    hdul[0].header = header
    hdul[0].data = data
    
    if outname is None:
        return hdul
    with utils.ignore_fits_warnings():
        hdul.writeto(outname, overwrite=True)


def generate_combined_map(search_dir, version_str,
                          subdir='proj_tweaked_images', use_outer=False):
    inner_outer = "_O_" if use_outer else "_I_"
    work_dirs = [
            os.path.join(search_dir, f) for f in sorted(os.listdir(search_dir))
                if version_str in f and inner_outer in f]
    print(("Outer" if use_outer else "Inner") + " FOV")
    for work_dir in work_dirs:
        print(work_dir)
    
    all_errors_x = []
    all_errors_y = []
    all_px_x = []
    all_px_y = []
    
    for work_dir in work_dirs:
        with open(os.path.join(work_dir, 'stars_db_r2.pkl'), 'rb') as f:
            series, sbf = pickle.load(f)
        files = utils.collect_files(os.path.join(work_dir, subdir), separate_detectors=False)
        _, _, px_x, px_y, errors_x, errors_y = series_errors(series, files)
        
        all_errors_x.extend(errors_x)
        all_errors_y.extend(errors_y)
        
        all_px_x.extend(px_x)
        all_px_y.extend(px_x)
    
    errors_x = np.array(errors_x)
    errors_y = np.array(errors_y)
    errors = np.sqrt(errors_x**2 + errors_y**2)
    px_x = np.array(px_x)
    px_y = np.array(px_y)
    
    filter = errors < 2
    err_x, err_y, err_px, err_py = calc_binned_err_components(
            px_x[filter], px_y[filter], errors_x[filter], errors_y[filter],
            ret_coords=True)
    
    display(HTML("<h3>Merged error map</h3>"))
    
    plt.figure(figsize=(15, 5))
    plt.subplot(131)
    plt.imshow(err_x, vmin=-0.8, vmax=0.8, cmap='bwr', origin='lower')
    plt.title("X offset table")
    plt.subplot(132)
    plt.imshow(err_y, vmin=-0.8, vmax=0.8, cmap='bwr', origin='lower')
    plt.colorbar(ax=plt.gcf().axes[:2]).set_label("Distortion (px)")
    plt.title("Y offset table")
    plt.subplot(133)
    plt.imshow(np.sqrt(err_x**2 + err_y**2), vmin=0, vmax=1, origin='lower')
    plt.colorbar().set_label("Distortion amplitude (px)")
    plt.title("Error magnitude")
    plt.suptitle("Unsmoothed")
    plt.show()
    
    err_x = filter_distortion_table(err_x)
    err_y = filter_distortion_table(err_y)
    
    plt.figure(figsize=(15, 5))
    plt.subplot(131)
    plt.imshow(err_x, vmin=-0.8, vmax=0.8, cmap='bwr', origin='lower')
    plt.title("X offset table")
    plt.subplot(132)
    plt.imshow(err_y, vmin=-0.8, vmax=0.8, cmap='bwr', origin='lower')
    plt.colorbar(ax=plt.gcf().axes[:2]).set_label("Distortion (px)")
    plt.title("Y offset table")
    plt.subplot(133)
    plt.imshow(np.sqrt(err_x**2 + err_y**2), vmin=0, vmax=1, origin='lower')
    plt.colorbar().set_label("Distortion amplitude (px)")
    plt.title("Error magnitude")
    plt.suptitle("Smoothed")
    plt.show()

    return err_x, err_y, err_px, err_py, work_dirs


def _write_combined_map(err_x, err_y, ifile, *ofiles, collect_wcs=False,
                        err_px=None, err_py=None):
    with utils.ignore_fits_warnings(), fits.open(ifile) as hdul:
        if len(hdul) > 1:
            hdul[1].data = -err_x.astype(hdul[1].data.dtype)
            hdul[2].data = -err_y.astype(hdul[2].data.dtype)
        else:
            hdul = add_distortion_table(
                ifile, None, err_x, err_y, err_px, err_py)
        for ofile in ofiles:
            hdul.writeto(ofile)
        if collect_wcs:
            return WCS(hdul[0].header, hdul, key='A')


def write_combined_maps(err_x, err_y, work_dirs, *out_dirs,
                        delete_existing=False, collect_wcses=False,
                        err_px=None, err_py=None):
    """
    Write a merged error map to many files
    
    Parameters
    ----------
    err_x, err_y : ``np.ndarray``
        The merged error map, to be written directly into the correct HDUs
    work_dirs : ``list`` of ``str``
        A list of input directories, each containing files that should receive
        the merged maps.
    out_dirs : one or multiple ``list``s of ``str``
        The output directories. Each file can be written to multiple output
        directories. If ``work_dirs`` is length N, and each file is to be
        written into M directories, then M lists should be provided, each
        containing N entries.
    delete_existing : ``bool``
        Whether to delete existing output directories
    """
    ifiles = []
    ofiles = [[] for x in out_dirs]
    for i, work_dir in enumerate(work_dirs):
        files = utils.collect_files(work_dir, separate_detectors=False)
        ifiles.extend(files)
        for j, out_dir in enumerate(out_dirs):
            ofiles[j].extend([
                os.path.join(out_dir[i], os.path.basename(f)) for f in files])

    for out_dir in chain(*out_dirs):
        if os.path.exists(out_dir):
            if delete_existing:
                shutil.rmtree(out_dir)
        os.makedirs(out_dir, exist_ok=True)

    wcses = {}
    for i in tqdm(range(len(ifiles))):
        ifile = ifiles[i]
        ofile = [of[i] for of in ofiles]
        wcs = _write_combined_map(err_x, err_y, ifile, *ofile,
            collect_wcs=collect_wcses, err_px=err_px, err_py=err_py)
        if collect_wcses:
            wcses[utils.to_timestamp(ifile)] = wcs
    if collect_wcses:
        return wcses


def series_errors(series, files, and_longitudes=False, wcses=None):
    if wcses is None or and_longitudes:
        if wcses is None:
            wcses = {}
        if and_longitudes:
            tstamp_to_longitude = {}
        for f in files:
            with utils.ignore_fits_warnings(), fits.open(f) as hdul:
                hdr = hdul[0].header
                tstamp = utils.to_timestamp(f)
                wcses[tstamp] = WCS(hdr, hdul, key='A')
                if and_longitudes:
                    tstamp_to_longitude[tstamp] = (np.arctan2(float(hdr['haey_obs']), float(hdr['haex_obs'])) * 180 / np.pi) % 360
    errors = []
    errors_x = []
    errors_y = []
    px_x = []
    px_y = []
    longitudes = []
    missing_keys = []
    for k, seq in series.items():
        ra, dec = k
        
        x_comp = []
        y_comp = []
        xs = []
        ys = []
        for p in seq:
            try:
                wcs = wcses[p[2]]
                if and_longitudes:
                    lon = tstamp_to_longitude[p[2]]
            except KeyError:
                missing_keys.append(p[2])
                continue
            xs.append(p[0])
            ys.append(p[1])
            x, y = wcs.all_world2pix(ra, dec, 0)
            x_comp.append(x)
            y_comp.append(y)
            if and_longitudes:
                longitudes.append(lon)
        xs = np.array(xs)
        ys = np.array(ys)
        x_comp = np.array(x_comp)
        y_comp = np.array(y_comp)
        
        dx = xs - x_comp
        dy = ys - y_comp
        dr = np.sqrt(dx**2 + dy**2)
        errors.extend(dr)
        errors_x.extend(dx)
        errors_y.extend(dy)
        px_x.extend(xs)
        px_y.extend(ys)
    errors = np.array(errors)
    errors_x = np.array(errors_x)
    errors_y = np.array(errors_y)
    px_x = np.array(px_x)
    px_y = np.array(px_y)
    longitudes = np.array(longitudes)
    
    if len(missing_keys):
        print(f"In error calcs, did not find files for times {missing_keys}")
    
    ret = np.sqrt(np.mean(np.square(errors))), errors, px_x, px_y, errors_x, errors_y
    if and_longitudes:
        ret = ret + (longitudes,)
    return ret