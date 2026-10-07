"""Offline failure and retry checks. No network or publishing credentials."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

CI=Path(sys.argv.pop(1)).resolve()
sys.path.insert(0,str(CI))
import publisher


class FakeGitHub:
    def __init__(self):
        self.release=None;self.fail_upload=False;self.patch_calls=0;self.latest=None
    def request(self,method,path,data=None):
        if path=='/releases/latest':return self.latest
        if method=='GET':return copy.deepcopy(self.release)
        if method=='POST':
            assert self.release is None
            self.release=dict(id=17,assets=[],html_url='https://github.com/PlaneSlate/liveries/releases/tag/'+data['tag_name'],**data)
        else:
            self.patch_calls+=1;self.release.update(data)
        return copy.deepcopy(self.release)
    def upload(self,release_id,file):
        if self.fail_upload:raise ValueError('simulated upload interruption')
        self.release['assets'].append(dict(name=file.name,size=file.stat().st_size,digest='sha256:'+publisher.digest(file)))
    def inventory_sequence(self,release):return release['sequence']


class Checks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        (self.path/'device-updates.json').write_text('{"schema":1}')
        (self.path/'ink-1.0.1.zip').write_bytes(b'payload')
        self.s=dict(version='1.0.1',sequence=1,job_id='0'*36,_snapshot_sha256='a'*64)
    def tearDown(self):self.temp.cleanup()
    def test_interrupted_upload_keeps_draft_and_retry_finishes(self):
        gh=FakeGitHub();gh.fail_upload=True
        with self.assertRaises(ValueError):publisher.publish(self.path,self.s,gh)
        self.assertTrue(gh.release['draft']);self.assertEqual(gh.patch_calls,0)
        gh.fail_upload=False;publisher.publish(self.path,self.s,gh)
        self.assertFalse(gh.release['draft']);self.assertEqual(gh.patch_calls,1)
        publisher.publish(self.path,self.s,gh)
        self.assertEqual(gh.patch_calls,1);self.assertEqual(len(gh.release['assets']),2)
    def test_user_draft_never_replaced(self):
        gh=FakeGitHub();gh.release=dict(body='Manual first release',draft=True)
        with self.assertRaises(ValueError):publisher.publish(self.path,self.s,gh)
        self.assertEqual(gh.patch_calls,0)
    def test_changed_asset_never_replaced(self):
        gh=FakeGitHub();publisher.publish(self.path,self.s,gh)
        (self.path/'ink-1.0.1.zip').write_bytes(b'changed')
        with self.assertRaises(ValueError):publisher.publish(self.path,self.s,gh)
        self.assertEqual(gh.patch_calls,1)
    def test_rollback_blocked(self):
        gh=FakeGitHub();gh.latest={'tag_name':'liveries-2.0.0'}
        with self.assertRaises(ValueError):publisher.publish(self.path,self.s,gh)
        self.assertIsNone(gh.release)
    def test_archive_normalization_is_reproducible(self):
        a=self.path/'one.zip';b=self.path/'two.zip'
        for path,date in [(a,(2020,1,1,0,0,0)),(b,(2026,10,5,12,0,0))]:
            with zipfile.ZipFile(path,'w') as z:z.writestr(zipfile.ZipInfo('a.png',date),b'PNG')
            publisher.normalize_zip(path)
        self.assertEqual(a.read_bytes(),b.read_bytes())
    def test_sequence_rollback_blocked(self):
        gh=FakeGitHub();gh.latest={'tag_name':'liveries-1.0.0','sequence':2}
        with self.assertRaises(ValueError):publisher.publish(self.path,self.s,gh)
        self.assertIsNone(gh.release)
    def test_server_checksum_failure_keeps_draft(self):
        gh=FakeGitHub();upload=gh.upload
        def broken(release_id,file):
            upload(release_id,file);gh.release['assets'][-1]['digest']='sha256:'+'0'*64
        gh.upload=broken
        with self.assertRaises(ValueError):publisher.publish(self.path,self.s,gh)
        self.assertTrue(gh.release['draft']);self.assertEqual(gh.patch_calls,0)
    def test_unsafe_original_names_blocked(self):
        for name in ('../A320_BAW.png','A320_BAW.png/evil','C:\\x.png','A320_x.png.exe','x.png'):
            self.assertFalse(publisher.valid_name(name))
    def test_cache_revalidates_content_and_recovers_corruption(self):
        source=self.path/'store';data=b'original';sha=hashlib.sha256(data).hexdigest()
        key='originals/sha256/'+sha+'.png';(source/key).parent.mkdir(parents=True);(source/key).write_bytes(data)
        cache=publisher.CachedOriginals(publisher.LocalR2(source),self.path/'cache')
        cache.get(key,self.path/'first',sha,len(data));cache.get(key,self.path/'second',sha,len(data))
        self.assertEqual((cache.hits,cache.misses),(1,1))
        (cache.root/(sha+'.png')).write_bytes(b'corrupt!')
        cache.get(key,self.path/'third',sha,len(data))
        self.assertEqual((self.path/'third').read_bytes(),data);self.assertEqual(cache.misses,2)
    def test_cache_wrong_size_and_bad_store_fail_closed(self):
        class Bad:
            def get(self,key,target,checksum,max_bytes):target.write_bytes(b'bad')
        cache=publisher.CachedOriginals(Bad(),self.path/'cache');sha='a'*64
        with self.assertRaises(ValueError):cache.get('originals/sha256/'+sha+'.png',self.path/'target',sha,3)
        self.assertFalse((cache.root/(sha+'.png')).exists())


unittest.main()
