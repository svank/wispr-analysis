from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from math import ceil
import os
import warnings

from astropy.io import fits
import numpy as np
import scipy.signal


def to_timestamp(datestring, as_datetime=False):
    # Check if we got a filename
    if datestring.endswith('.fits'):
        # Grab just the filename if it's a full path
        if '/' in datestring:
            datestring = datestring.split('/')[-1]
        # Extract the timestamp part of the standard WISPR filename
        datestring = datestring.split('_')[3]
    try:
        dt = datetime.strptime(
                datestring, "%Y%m%dT%H%M%S")
    except ValueError:
        dt = datetime.strptime(
                datestring, "%Y-%m-%dT%H:%M:%S.%f")
    dt = dt.replace(tzinfo=timezone.utc)
    if as_datetime:
        return dt
    return dt.timestamp()


def get_PSP_path(data_dir):
    files = collect_files(data_dir, separate_detectors=False, order='date-avg',
            include_headers=True)
    return get_PSP_path_from_headers([f[1] for f in files])


def get_PSP_path_from_headers(headers):
    times = []
    positions = []
    vs = []
    for header in headers:
        times.append(to_timestamp(header['DATE-AVG']))
        positions.append((
            header['HCIx_OBS'],
            header['HCIy_OBS'],
            header['HCIz_OBS']))
        vs.append((
            header['HCIx_VOB'],
            header['HCIy_VOB'],
            header['HCIz_VOB']))
    return np.array(times), np.array(positions), np.array(vs)


def collect_files(top_level_dir, separate_detectors=True, order=None,
        include_sortkey=False, include_headers=False, between=(None, None)):
    """Given a directory, returns a sorted list of all WISPR FITS files.
    
    Subdirectories are searched, so this lists all WISPR files for an
    encounter when they are in separate subdirectories by date. By default,
    returns two lists, one for each detector. Set `separate_detectors` to False
    to return only a single, sorted list. Returned items are full paths
    relative to the given directory.
    
    Invalid files are omitted. The expected structure is that subdirectories
    have names starting with "20" (i.e. for the year 20XX), and file names
    should be in the standard formated provided by the WISPR team.
    
    Files are ordered by the value of the FITS header key provided as the
    `order` argument. Set `order` to None to sort by filename instead (which is
    implicitly DATE-BEG, as that is contained in the filenames).
    """
    i_files = []
    o_files = []
    subdirs = []
    # Find all valid subdirectories.
    for fname in os.listdir(top_level_dir):
        path = f"{top_level_dir}/{fname}"
        if os.path.isdir(path) and fname.startswith('20'):
            subdirs.append(path)
    if len(subdirs) == 0:
        subdirs.append(top_level_dir)

    for dir in subdirs:
        for file in os.listdir(dir):
            if file[0:3] != 'psp' or file[-5:] != '.fits':
                continue
            fname = f"{dir}/{file}"
            with ignore_fits_warnings():
                if order is None:
                    key = file.split('_')[3]
                    if include_headers:
                        header = fits.getheader(fname)
                else:
                    header = fits.getheader(fname)
                    key = header[order]
            
            if ((between[0] is not None and key < between[0])
                    or (between[1] is not None and key > between[1])):
                continue
            
            if include_headers:
                item = (key, fname, header)
            else:
                item = (key, fname)
            
            if fname[-9] == '1':
                i_files.append(item)
            else:
                o_files.append(item)
    
    def cleaner(v):
        if not include_sortkey:
            if include_headers:
                return v[1:]
            else:
                return v[1]
        return v

    if separate_detectors:
        i_files = sorted(i_files)
        o_files = sorted(o_files)
        return [cleaner(v) for v in i_files], [cleaner(v) for v in o_files]
    
    files = sorted(i_files + o_files)
    return [cleaner(v) for v in files]


def ensure_data(input, header=True):
    if isinstance(input, str):
        with ignore_fits_warnings():
            data, hdr = fits.getdata(input, header=True)
    elif isinstance(input, list) or isinstance(input, tuple):
        data, hdr = input
    else:
        data = input
        hdr = None
    
    if header:
        return data, hdr
    return data


def get_hann_rolloff(shape, rolloff):
    shape = np.atleast_1d(shape)
    rolloff = np.atleast_1d(rolloff)
    if len(rolloff) == 1:
        rolloff = np.concatenate([rolloff] * len(shape))
    elif len(rolloff) != len(shape):
        raise ValueError("`rolloff` must be a scalar or match the length of `shape`")
    hann_widths = rolloff * 2
    if np.any(hann_widths <= 2) or np.any(hann_widths != hann_widths.astype(int)):
        raise ValueError("`rolloff` should be > 1 and an integer or half-integer")
    mask = np.ones(shape)
    for i, hann_width in zip(range(len(shape)), hann_widths):
        if hann_width / 2 >= shape[i]:
            raise ValueError(f"Rolloff size of {hann_width/2} is too large for"
                             f"dimension {i} with size {shape[i]}")
        if hann_width >= shape[i]:
            warnings.warn(f"Rolloff size of {hann_width/2} doesn't fit for "
                          f"dimension {i} with size {shape[i]}---the two ends overlap")
        window = scipy.signal.windows.hann(hann_width)[:ceil(hann_width/2)]
        mask_indices = [slice(None)] * len(shape)
        mask_indices[i] = slice(0, window.size)
        window_indices = [None] * len(shape)
        window_indices[i] = slice(None)
        mask[tuple(mask_indices)] = (
                mask[tuple(mask_indices)] * window[tuple(window_indices)])
        
        mask_indices[i] = slice(-window.size, None)
        window_indices[i] = slice(None, None, -1)
        mask[tuple(mask_indices)] = (
                mask[tuple(mask_indices)] * window[tuple(window_indices)])
    return mask


@contextmanager
def ignore_fits_warnings():
    with warnings.catch_warnings():
        warnings.filterwarnings(
                action='ignore', message=".*'BLANK' keyword.*")
        warnings.filterwarnings(
                action='ignore', message=".*datfix.*")
        yield