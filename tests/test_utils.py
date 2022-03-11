from .. import utils

from datetime import datetime, timezone
import os
import tempfile

from astropy.io import fits
import numpy as np
import pytest
import warnings


def test_to_timestamp():
    assert (utils.to_timestamp('2021-02-03T12:13:14.5')
            == datetime(
                2021, 2, 3, 12, 13, 14, 500000, timezone.utc).timestamp())
    assert (utils.to_timestamp('20210203T121314')
            == datetime(2021, 2, 3, 12, 13, 14, 0, timezone.utc).timestamp())
    assert (utils.to_timestamp('path/psp_L3_wispr_20210111T083017_V1_1221.fits')
            == datetime(2021, 1, 11, 8, 30, 17, 0, timezone.utc).timestamp())
    assert (utils.to_timestamp(
        'path/psp_L3_wispr_20210111T083017_V1_1221.fits', as_datetime=True)
            == datetime(2021, 1, 11, 8, 30, 17, 0, timezone.utc))
    assert isinstance(utils.to_timestamp(
        'path/psp_L3_wispr_20210111T083017_V1_1221.fits', as_datetime=True),
            datetime)


def test_get_PSP_path():
    dir_path = (os.path.dirname(__file__)
                + '/test_data/WISPR_files_headers_only/')
    times, positions, vs = utils.get_PSP_path(dir_path)
    assert times.size == positions.shape[0] == vs.shape[0]
    assert np.all(times[1:] > times[:-1])
    assert positions.shape[1] == 3
    assert vs.shape[1] == 3


def test_collect_files():
    dir_path = (os.path.dirname(__file__)
                + '/test_data/WISPR_files_headers_only/')
    files = utils.collect_files(dir_path, separate_detectors=True)
    files_avg = utils.collect_files(dir_path, separate_detectors=True,
            order='DATE-AVG')
    files_together = utils.collect_files(dir_path, separate_detectors=False)
    files_together_avg = utils.collect_files(dir_path,
            separate_detectors=False, order='DATE-AVG')
    
    for file_list in (files, files_avg):
        assert len(file_list) == 2
        assert len(file_list[0]) == 60
        assert len(file_list[1]) == 58
        for file in file_list[0]:
            assert 'V3_1' in file
        for file in file_list[1]:
            assert 'V3_2' in file
    
    for file_list in (files_together, files_together_avg):
        assert len(file_list) == 118
    
    for file_list, key in (
            (files[0], 'DATE-BEG'),
            (files[1], 'DATE-BEG'),
            (files_together, 'DATE-BEG'),
            (files_avg[0], 'DATE-AVG'),
            (files_avg[1], 'DATE-AVG'),
            (files_together_avg, 'DATE-AVG')):
        last_timestamp = -1
        for file in file_list:
            header = fits.getheader(file)
            timestamp = datetime.strptime(
                    header[key], "%Y-%m-%dT%H:%M:%S.%f").timestamp()
            assert timestamp > last_timestamp
            last_timestamp = timestamp


def test_collect_files_with_headers():
    dir_path = (os.path.dirname(__file__)
                + '/test_data/WISPR_files_headers_only/')
    file_list = utils.collect_files(dir_path + '20181101', include_headers=True,
            separate_detectors=False)
    assert len(file_list) == 58
    assert len(file_list[0]) == 2
    assert isinstance(file_list[0][1], fits.Header)
    
    file_list = utils.collect_files(dir_path + '20181101', include_headers=True,
            include_sortkey=True, separate_detectors=False,
            order='date-avg')
    assert len(file_list) == 58
    assert len(file_list[0]) == 3
    assert isinstance(file_list[0][2], fits.Header)
    for sortkey, file, header in file_list:
        assert header['DATE-AVG'] == sortkey


def test_collect_files_between():
    dir_path = (os.path.dirname(__file__)
                + '/test_data/WISPR_files_headers_only/')
    file_list = utils.collect_files(dir_path, separate_detectors=False,
            between=('20181102T000000', None))
    assert len(file_list) == 60
    
    file_list = utils.collect_files(dir_path, separate_detectors=False,
            between=(None, '20181102T000000'))
    assert len(file_list) == 58
    
    file_list = utils.collect_files(dir_path, separate_detectors=False,
            between=('20181101T103000', '20181102T000000'))
    assert len(file_list) == 34


def test_collect_files_filters():
    dir_path = (os.path.dirname(__file__)
                + '/test_data/WISPR_files_headers_only/')
    file_list = utils.collect_files(dir_path, separate_detectors=False,
            include_headers=True)
    all_values = np.array([f[1]['dsun_obs'] for f in file_list])
    
    for lower in [32067077000, None]:
        for upper in [34213000000, None]:
            file_list = utils.collect_files(dir_path, separate_detectors=False,
                    filters=[('dsun_obs', lower, upper)], include_headers=True)
            
            expected = np.ones_like(all_values)
            if lower is not None:
                expected *= all_values >= lower
            if upper is not None:
                expected *= all_values <= upper
            
            assert len(file_list) == np.sum(expected)
            headers = [f[1] for f in file_list]
            for h in headers:
                if lower is not None:
                    assert float(h['dsun_obs']) >= lower
                if upper is not None:
                    assert float(h['dsun_obs']) <= upper

def test_collect_files_two_filters():
    dir_path = (os.path.dirname(__file__)
                + '/test_data/WISPR_files_headers_only/')
    file_list = utils.collect_files(dir_path, separate_detectors=False,
            include_headers=True)
    all_values1 = np.array([f[1]['dsun_obs'] for f in file_list])
    all_values2 = np.array([f[1]['xposure'] for f in file_list])
    
    file_list = utils.collect_files(dir_path, separate_detectors=False,
            filters=[('dsun_obs', 32067077000, 34213000000),
                     ('xposure', 3.3e10, None)],
            include_headers=True)
    
    e = (all_values1 >= 32067077000) * (all_values1 <= 34213000000)
    expected = e * (all_values2 >= 3.3e10)
    # Ensure that the values chosen for the second filter actually have an effect
    assert np.sum(e) != np.sum(expected)
    
    assert len(file_list) == np.sum(expected)
    headers = [f[1] for f in file_list]
    for h in headers:
        assert float(h['dsun_obs']) >= 32067077000
        assert float(h['dsun_obs']) <= 34213000000
        assert float(h['xposure']) <= 3.3e10


def test_ensure_data():
    data = np.arange(10)
    data_out, h = utils.ensure_data(data)
    assert data is data_out
    
    data_out, h = utils.ensure_data((data, 0))
    assert data is data_out
    assert h == 0
    
    data_out = utils.ensure_data(data, header=False)
    assert data is data_out
    
    with tempfile.TemporaryDirectory() as td:
        file = os.path.join(td, 'file.fits')
        fits.writeto(file, data)
        
        data_out, h = utils.ensure_data(file)
        assert np.all(data_out == data)
        assert isinstance(h, fits.Header)


def test_get_hann_rolloff_1d():
    window = utils.get_hann_rolloff(50, 10)
    assert window.shape == (50,)
    np.testing.assert_equal(window[10:-10], 1)
    np.testing.assert_array_less(window[:10], 1)
    np.testing.assert_array_less(window[-10:], 1)
    np.testing.assert_equal(window[:10], window[-10:][::-1])


def test_get_hann_rolloff_2d():
    window = utils.get_hann_rolloff((20, 20), 5)
    assert window.shape == (20, 20)
    np.testing.assert_equal(window[5:-5, 5:-5], 1)
    
    np.testing.assert_array_less(window[:5, :], 1)
    np.testing.assert_array_less(window[:, :5], 1)
    np.testing.assert_array_less(window[-5:, :], 1)
    np.testing.assert_array_less(window[:, -5:], 1)
    
    np.testing.assert_equal(window[:5, :], window[-5:, :][::-1, :])
    np.testing.assert_equal(window[:, :5], window[:, -5:][:, ::-1])
    
    window = utils.get_hann_rolloff((20, 25), (7, 3))
    assert window.shape == (20, 25)
    np.testing.assert_equal(window[7:-7, 3:-3], 1)
    
    np.testing.assert_array_less(window[:7, :], 1)
    np.testing.assert_array_less(window[:, :3], 1)
    np.testing.assert_array_less(window[-7:, :], 1)
    np.testing.assert_array_less(window[:, -3:], 1)
    
    np.testing.assert_equal(window[:7, :], window[-7:, :][::-1, :])
    np.testing.assert_equal(window[:, :3], window[:, -3:][:, ::-1])


def test_get_hann_rolloff_3d():
    window = utils.get_hann_rolloff((20, 20, 20), 5)
    assert window.shape == (20, 20, 20)
    np.testing.assert_equal(window[5:-5, 5:-5, 5:-5], 1)
    
    np.testing.assert_array_less(window[:5, :, :], 1)
    np.testing.assert_array_less(window[:, :5, :], 1)
    np.testing.assert_array_less(window[:, :, :5], 1)
    np.testing.assert_array_less(window[-5:, :, :], 1)
    np.testing.assert_array_less(window[:, -5:, :], 1)
    np.testing.assert_array_less(window[:, :, -5:], 1)
    
    np.testing.assert_equal(window[:5, :, :], window[-5:, :, :][::-1, :, :])
    np.testing.assert_equal(window[:, :5, :], window[:, -5:, :][:, ::-1, :])
    np.testing.assert_equal(window[:, :, :5], window[:, :, -5:][:, :, ::-1])
    
    window = utils.get_hann_rolloff((20, 25, 15), (7, 3, 4))
    assert window.shape == (20, 25, 15)
    np.testing.assert_equal(window[7:-7, 3:-3, 4:-4], 1)
    
    np.testing.assert_array_less(window[:7, :, :], 1)
    np.testing.assert_array_less(window[:, :3, :], 1)
    np.testing.assert_array_less(window[:, :, :4], 1)
    np.testing.assert_array_less(window[-7:, :, :], 1)
    np.testing.assert_array_less(window[:, -3:, :], 1)
    np.testing.assert_array_less(window[:, :, -4:], 1)
    
    np.testing.assert_equal(window[:7, :, :], window[-7:, :, :][::-1, :, :])
    np.testing.assert_equal(window[:, :3, :], window[:, -3:, :][:, ::-1, :])
    np.testing.assert_equal(window[:, :, :4], window[:, :, -4:][:, :, ::-1])


def test_get_hann_rolloff_errors():
    # Too many rolloff sizes
    with pytest.raises(ValueError):
        utils.get_hann_rolloff(50, (20, 30))
    with pytest.raises(ValueError):
        utils.get_hann_rolloff((20, 30), (20, 30, 40))
    
    # Rolloffs that don't even fit in the window
    with pytest.raises(ValueError):
        utils.get_hann_rolloff(10, 20)
    with pytest.raises(ValueError):
        utils.get_hann_rolloff((10, 20), (3, 30))
    with pytest.raises(ValueError):
        utils.get_hann_rolloff((10, 20), (12, 3))
    
    # Rolloffs for which the two ends overlap
    with pytest.warns(Warning):
        utils.get_hann_rolloff(10, 8)
    with pytest.warns(Warning):
        utils.get_hann_rolloff((10, 20), (3, 12))
    with pytest.warns(Warning):
        utils.get_hann_rolloff((10, 20), (8, 3))
    
    # Check that the following does *not* cause a warning
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(Exception):
            utils.get_hann_rolloff((10, 20), (5, 8))
    
    # Too-small rolloff
    with pytest.raises(ValueError):
        utils.get_hann_rolloff(10, 1)
    
    # Non-integer rolloff
    with pytest.raises(ValueError):
        utils.get_hann_rolloff(10, 2.2)
    
