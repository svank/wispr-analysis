import copy
from datetime import datetime
from itertools import repeat
import itertools
import multiprocessing
import os
import tempfile

import astropy.units as u
from IPython.display import display, Video
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
from tqdm.contrib.concurrent import process_map

from . import composites
from . import data_cleaning
from . import plot_utils
from . import utils


cmap = copy.copy(plt.cm.Greys_r)
cmap.set_bad('black')

    
def make_WISPR_video(data_dir, between=(None, None), trim_threshold=12*60*60,
        level_preset=None, vmin=None, vmax=None, duration=15, fps=20,
        destreak=True):
    """
    Renders a video of a WISPR data sequence, in a composite field of view.
    
    The video plays back at a constant rate in time (as opposed to showing each
    WISPR image for one frame, for instance). This gives the best
    representation of the physical speeds of every feature in the image, but
    can produce jittery videos when the WISPR imaging cadence drops. At each
    point in time, the most recent preceeding images are shown for the inner
    and outer imagers (or no image is shown if none is available or the most
    recent image is over a day before the frame's timestamp).
    
    The rendered video is displayed in the Jupyter environment. No other output
    modes are currently supported.
    
    Arguments
    ---------
    data_dir : str
        Directory containing WISPR fits files. Will be scanned by
        `utils.collect_files`
    between : tuple
        A beginning and end timestamp of the format 'YYYYMMDDTHHMMSS'. Only the
        files between these timestamps will be included in the video. Either or
        both timestamp can be `None`, to place no limit on the beginning or end
        of the video.
    trim_threshold : float
        After filtering is done with the `between` values, the video start and
        end times are determined by scanning the time gaps between successive
        images. Starting from the midpoint of the data sequence, the time range
        being shown expands out in both directions to the first time gap that
        exceeds `trim_thresholds` (a value in seconds). Set to None to disable.
    level_preset : str or int
        The colorbar range is set with default values appropriate to L2 or L3
        data. This is autodetected from the files but can be overridden with
        this argument.
    vmin, vmax : float
        Colorbar ranges can be set explicitly to override the defaults
    duration : float
        Duration of the output video in seconds
    fps : float
        Number of frames per second in the video
    destreak : boolean
        Whether to enable the debris streak removal algorithm
    """
    i_files, o_files = utils.collect_files(
            data_dir, separate_detectors=True, include_sortkey=True,
            include_headers=True, between=between)
    i_files = [(utils.to_timestamp(f[0]), f[1], f[2]) for f in i_files]
    o_files = [(utils.to_timestamp(f[0]), f[1], f[2]) for f in o_files]
    i_tstamps = [f[0] for f in i_files]
    o_tstamps = [f[0] for f in o_files]
    tstamps = sorted(itertools.chain(i_tstamps, o_tstamps))   
    
    # Do this before setting the time range, so the full s/c trajectory is
    # visible in the inset plot
    path_times, path_positions, _ = utils.get_PSP_path_from_headers(
                    [v[-1] for v in sorted(itertools.chain(i_files, o_files))])
    
    if trim_threshold is not None:
        # Set our time range by computing delta-ts and keeping the middle
        # segment, trimming from the start and end any periods with more than
        # some threshold between frames.
        t_deltas = np.diff(tstamps)
        midpoint = len(t_deltas) // 2
        i = np.nonzero(t_deltas[:midpoint] > trim_threshold)[0]
        if len(i):
            i = i[-1]
        else:
            i = 0
        t_start = tstamps[i+1]

        j = np.nonzero(t_deltas[midpoint:] > trim_threshold)[0]
        if len(j):
            j = j[0] + midpoint
        else:
            j = len(tstamps) - 1
        t_end = tstamps[j]

    i_files = [v for v in i_files if t_start <= v[0] <= t_end]
    o_files = [v for v in o_files if t_start <= v[0] <= t_end]
    i_tstamps = [f[0] for f in i_files]
    o_tstamps = [f[0] for f in o_files]
    # Remove the stale `tstamps` data
    del tstamps
    
    wcsh, naxis1, naxis2 = composites.gen_header(
            i_files[len(i_files)//2][2], o_files[len(o_files)//2][2])
    bounds = composites.find_collective_bounds(
            ([v[-1] for v in i_files[::3]], [v[-1] for v in o_files[::3]]),
            wcsh, ((33, 40, 42, 39), (20, 25, 26, 31)))

    frames = np.linspace(t_start, t_end, fps*duration)

    images = []
    # Determine the correct pair of images for each timestep
    for t in frames:
        i_cut = np.nonzero(
                (i_tstamps <= t) * (np.abs(i_tstamps - t) < 24 * 60 * 60))[0]
        if len(i_cut):
            i = i_cut[-1]
        else:
            i = None
        
        j_cut = np.nonzero(
                (o_tstamps <= t) * (np.abs(o_tstamps - t) < 24 * 60 * 60))[0]
        if len(j_cut):
            j = j_cut[-1]
        else:
            j = None
        images.append((i, j, t))

    # Sometimes the same image pair is chosen for multiple time steps---the
    # output will be the same execpt for the overlaid time stamp, so we can
    # render multiple frames from one reprojection call and save time. Convert
    # our list to a list of (unique_image_pair, list_of_timesteps)
    images = [(pair, [d[-1] for d in data])
              for pair, data in itertools.groupby(images, lambda x: x[0:2])]
    
    level_preset = plot_utils.parse_level_preset(level_preset, i_files[0][2])
    
    colorbar_data = plot_utils.COLORBAR_PRESETS[level_preset]
    
    if vmin is None:
        vmin = min(*[d[0] for d in colorbar_data.values()])
    if vmax is None:
        vmax = max(*[d[1] for d in colorbar_data.values()])
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Rather than explicitly passing ~15 different variables to the
        # per-frame plotting function, let's be hacky and just send the whole
        # dictionary of local variables, which is mostly things we need to send
        # anyway.
        arguments = zip(images, repeat(locals()))
        process_map(draw_WISPR_video_frame, arguments, total=len(images))
        os.system(f"ffmpeg -loglevel error -r {fps} -pattern_type glob"
                  f" -i '{tmpdir}/*.png' -c:v libx264 -pix_fmt yuv420p"
                  f" -vf 'pad=ceil(iw/2)*2:ceil(ih/2)*2' {tmpdir}/out.mp4")
        display(Video(
            f"{tmpdir}/out.mp4",
            embed=True,
            html_attributes="controls loop"))


def draw_WISPR_video_frame(data):
    (pair, timesteps), local_vars = data
    # `locals` is not updatable (except at the lop level where locals() is just
    # globals()), so we have to update globals() instead. Since we're
    # multiprocessing, those global variables will be thrown away when this
    # process ends.
    globals().update(local_vars)
    i, j = pair
    
    if i is None:
        input_i = None
    else:
        if destreak and 0 < i < len(i_files) - 1:
            input_i = data_cleaning.dust_streak_filter(
                    i_files[i-1][1], i_files[i][1], i_files[i+1][1],
                    sliding_window_stride=3)
            input_i = (input_i, i_files[i][2])
        else:
            input_i = i_files[i][1]
    
    if j is None:
        input_o = None
    else:
        if destreak and 0 < j < len(o_files) - 1:
            input_o = data_cleaning.dust_streak_filter(
                    o_files[j-1][1], o_files[j][1], o_files[j+1][1],
                    sliding_window_stride=3)
            input_o = (input_o, o_files[j][2])
        else:
            input_o = o_files[j][1]
    
    c, wcs_plot = composites.gen_composite(
        # Even if we're blanking one of the images, a header is still
        # needed (for now...)
        input_i if input_i is not None else i_files[0][1],
        input_o if input_o is not None else o_files[0][1],
        bounds=bounds,
        wcsh=wcsh, naxis1=naxis1, naxis2=naxis2,
        blank_i=(input_i is None), blank_o=(input_o is None))
    
    with plt.style.context('dark_background'):
        for t in timesteps:
            fig = plt.figure(figsize=(10, 7.5), dpi=140)
            ax = fig.add_subplot(111, projection=wcs_plot)

            im = ax.imshow(c, cmap=cmap, origin='lower',
                           norm=matplotlib.colors.PowerNorm(
                               gamma=0.5, vmin=0, vmax=vmax))
            text = ax.text(20, 20,
                    datetime.fromtimestamp(t).strftime("%Y-%m-%d, %H:%M"),
                    color='white')

            fig.subplots_adjust(top=0.96, bottom=0.10,
                    left=0.05, right=0.98)
            lon, lat = ax.coords
            lat.set_ticks(np.arange(-90, 90, 10) * u.degree)
            lon.set_ticks(np.arange(-180, 180, 15) * u.degree)
            lat.set_major_formatter('dd')
            lon.set_major_formatter('dd')
            ax.set_xlabel("Helioprojective Longitude")
            ax.set_ylabel("Helioprojective Latitude")
            ax.coords.grid(color='white', alpha=0.2)
            
            ax_orbit = fig.add_axes((.13, .13, .12, .12))
            ax_orbit.margins(.1, .1)
            ax_orbit.set_aspect('equal')
            ax_orbit.scatter(
                    path_positions[:, 0],
                    path_positions[:, 1],
                    s=1, color='.4', marker='.')
            sc_x = np.interp(t, path_times, path_positions[:, 0])
            sc_y = np.interp(t, path_times, path_positions[:, 1])
            ax_orbit.scatter(sc_x, sc_y, s=6, color='1')
            ax_orbit.scatter(0, 0, color='yellow')
            ax_orbit.xaxis.set_visible(False)
            ax_orbit.yaxis.set_visible(False)
            ax_orbit.set_title("S/C position", fontdict={'fontsize': 9})
            for spine in ax_orbit.spines.values():
                spine.set_color('.4')

            fig.savefig(f"{tmpdir}/{t:035.20f}.png")
            plt.close(fig)