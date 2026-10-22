import mmap
from pathlib import Path
import tempfile
import unittest
from ketris_mig.install_runtime import install_library
class InstallTests(unittest.TestCase):
    def test_running_library_mapping_survives_install_and_identical_restart(self):
        with tempfile.TemporaryDirectory() as root:
            src=Path(root)/'source.so';dest=Path(root)/'node/lib.so';dest.parent.mkdir();dest.write_bytes(b'old runtime bytes')
            with dest.open('rb') as fd,mmap.mmap(fd.fileno(),0,access=mmap.ACCESS_READ) as mapped:
                src.write_bytes(b'new runtime bytes');self.assertTrue(install_library(src,dest))
                self.assertEqual(mapped[:],b'old runtime bytes');self.assertEqual(dest.read_bytes(),b'new runtime bytes')
                inode=dest.stat().st_ino;self.assertFalse(install_library(src,dest));self.assertEqual(dest.stat().st_ino,inode)
