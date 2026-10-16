import importlib.util
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
import pytest

@pytest.fixture
def appmod(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = Mock()
    fake.status_code = 200
    fake.text = 'Ok.'
    fake.raise_for_status.return_value = None
    api = Mock()
    api.status_code = 200
    api.text = '2.11.0'
    api.raise_for_status.return_value = None
    session = Mock()
    session.get.side_effect = [fake, api]
    session.post.return_value = fake
    with patch('requests.Session', return_value=session):
        spec = importlib.util.spec_from_file_location('qflow_tested', str(Path(__file__).resolve().parents[1] / 'main.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod.CONFIG['DOWNLOAD_DIR'] = str(tmp_path)
    return mod

def test_uses_modern_qbit_api(appmod):
    m = appmod
    assert m.qbit._uses_start_stop()
    with patch.object(m.qbit, '_post') as post:
        m.qbit.resume('aabb')
        assert post.call_args.args[0] == 'torrents/start'
        assert m.qbit.add_torrent('magnet:?xt=urn:btih:abc')
        assert post.call_args.args[1]['stopped'] == 'true'

def test_legacy_api(appmod):
    m=appmod
    m.qbit.api_version=(2,10)
    with patch.object(m.qbit,'_post') as post:
        m.qbit.resume('aabb')
        assert post.call_args.args[0]=='torrents/resume'

def test_upload_move_only_after_verification(appmod, tmp_path):
    m=appmod
    data=tmp_path/'example.bin'
    data.write_bytes(b'abc')
    item=m.FileItem(torrent_hash='abcd', index=0, path=str(data), rel_path='example.bin', size=3, status=3)
    session=m.Session()
    session.add(item)
    session.commit()
    fid=item.id
    m.Session.remove()
    completed=Mock(returncode=0,stdout='',stderr='')
    verified=Mock(returncode=0,stdout='{"bytes": 3}',stderr='')
    with patch.object(m.subprocess,'run',side_effect=[completed,verified]), patch.object(m.qbit,'set_priority'):
        scheduler=m.Scheduler()
        scheduler.run_rclone(fid,str(data),'example.bin','abcd',0)
    assert not data.exists()
    assert m.Session().get(m.FileItem,fid).status==4
    m.Session.remove()

def test_upload_failure_preserves_local(appmod,tmp_path):
    m=appmod
    data=tmp_path/'fail.bin'
    data.write_bytes(b'abc')
    item=m.FileItem(torrent_hash='abcd', index=0, path=str(data), rel_path='fail.bin', size=3, status=3)
    session=m.Session()
    session.add(item)
    session.commit()
    fid=item.id
    m.Session.remove()
    with patch.object(m.subprocess,'run',return_value=Mock(returncode=1,stderr='offline',stdout='')):
        scheduler=m.Scheduler()
        scheduler.run_rclone(fid,str(data),'fail.bin','abcd',0)
    assert data.read_bytes()==b'abc'
    assert m.Session().get(m.FileItem,fid).status==2
    m.Session.remove()

def test_upload_restart_recovery(appmod):
    m=appmod
    session=m.Session()
    row=m.FileItem(torrent_hash='abcd',index=1,path='/nothing',rel_path='nothing',size=1,status=3)
    session.add(row)
    session.commit()
    fid=row.id
    m.Session.remove()
    m.Scheduler()
    assert m.Session().get(m.FileItem,fid).status==2
    m.Session.remove()
