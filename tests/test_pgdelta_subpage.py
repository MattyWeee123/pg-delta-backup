"""--subpage (pgdelta.subpage): only the 512-byte blocks of a page that differ from B's page cross the link.

Offered in HELLO and taken in ACCEPT only where the receiver builds on B's bytes (in place, or copy-patch), so a
session without it is unchanged on the wire (tests/pgdelta_frames.py). Each copy must equal S byte for byte; a
frame that claims more than it holds, or patches a page B doesn't hold whole, is refused; a wrong block is caught
by verification, because a patched page counts as written and is re-hashed whole.
"""
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pgdelta import protocol, sender as sender_module, subpage
from pgdelta.errors import ProtocolError, VerificationError
from pgdelta.inplace import CONTROL_FILE

from pgdelta_support import PAGE, snapshot, transfer, write_tree


def text_page(rng, n):
    row = lambda i: f'{i:010d}|{rng.randrange(10**6):06d}|{"%032x" % rng.getrandbits(128)}|'.encode()
    return b''.join(row(n * 1000 + i) for i in range(200))[:PAGE].ljust(PAGE, b' ')


class SubpageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.basis, self.output = self.root / 'S', self.root / 'B', self.root / 'O'
        rng = random.Random(56)
        old = b''.join(text_page(rng, n) for n in range(40))
        new = bytearray(old)
        for page in range(0, 40, 3):  # scattered: one block of each changed page
            new[page * PAGE + 700:page * PAGE + 720] = rng.randbytes(20)
        new[7 * PAGE:8 * PAGE] = rng.randbytes(PAGE)  # every block of one page
        grown = old[:5 * PAGE] + rng.randbytes(2 * PAGE + 100)  # two new pages and a short last one
        control = rng.randbytes(PAGE)
        write_tree(self.source, {'PG_VERSION': b'17\n', CONTROL_FILE: control, 'base/1/rel': bytes(new),
                                 'base/1/grown': grown, 'base/1/added': rng.randbytes(3 * PAGE)})
        write_tree(self.basis, {'PG_VERSION': b'17\n', CONTROL_FILE: rng.randbytes(PAGE), 'base/1/rel': old,
                                'base/1/grown': old[:5 * PAGE]})

    def check(self, outcome, copy):
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertIsNone(outcome.receiver_error, repr(outcome.receiver_error))
        self.assertEqual(snapshot(copy), snapshot(self.source))
        return outcome.sender['subpage'], outcome.sender['receiver']['subpage']

    def test_in_place_sends_only_the_changed_blocks(self):
        sent, received = self.check(transfer(self.source, self.basis, None, subpage=True,
                                             receiver_options={'in_place': True}), self.basis)
        self.assertEqual((sent['used'], received['accepted']), (True, True))
        self.assertGreater(sent['pages_partial'], 10)
        self.assertEqual(sent['blocks_sent'], sent['pages_partial'])  # one block of each scattered page
        self.assertGreaterEqual(sent['pages_whole'], 1)  # the page that changed throughout, and the new ones
        self.assertEqual((received['pages_partial'], received['blocks_received']),
                         (sent['pages_partial'], sent['blocks_sent']))
        self.assertGreater(sent['block_digest_bytes'], 0)
        self.assertEqual(sent['block_digest_bytes'], received['block_digest_bytes'])

    def test_a_new_copy_built_on_b_matches_and_sends_fewer_bytes(self):
        plain = transfer(self.source, self.basis, self.root / 'O-plain')
        sent, _ = self.check(transfer(self.source, self.basis, self.output, subpage=True), self.output)
        self.assertTrue(sent['used'])
        delta = transfer(self.source, self.basis, self.root / 'O-again', subpage=True)
        page_bytes = lambda o: o.sender['bytes_source_to_dest']['page_data']
        # The new files, the new pages and the page changed throughout go whole either way; the rest shrink.
        self.assertLess(page_bytes(delta), page_bytes(plain) * 0.5)
        self.assertIn('PAGE_DELTA', delta.sender['bytes_source_to_dest']['by_message_type'])
        self.assertIn('BLOCK_DIGESTS', delta.sender['bytes_dest_to_source']['by_message_type'])

    def test_with_zstd_on_worker_processes(self):
        sent, received = self.check(transfer(self.source, self.basis, self.output, subpage=True, compress='zstd',
                                             jobs=2, receiver_jobs=2), self.output)
        self.assertTrue(sent['used'] and received['accepted'])

    def test_gap_fill_declines_and_the_pages_go_whole(self):
        outcome = transfer(self.source, self.basis, self.output, subpage=True, receiver_options={'rebuild': 'gap-fill'})
        sent, received = self.check(outcome, self.output)
        self.assertEqual((sent['used'], received['accepted']), (False, False))
        self.assertNotIn('PAGE_DELTA', outcome.sender['bytes_source_to_dest']['by_message_type'])

    def test_without_subpage_nothing_is_offered(self):
        outcome = transfer(self.source, self.basis, self.output)
        self.assertIsNone(outcome.sender_error, repr(outcome.sender_error))
        self.assertNotIn('subpage', outcome.sender)
        self.assertNotIn('subpage', outcome.sender['receiver'])

    def test_a_frame_shorter_than_its_bitmap_is_refused(self):
        def short(self, first, data, old):
            return b'\x00\x03' + bytes(SUB_BLOCK_HALF)  # two blocks marked, half of one sent

        with mock.patch.object(sender_module._Sender, '_delta', short):
            outcome = transfer(self.source, self.basis, self.output, subpage=True)
        self.assertIsInstance(outcome.receiver_error, ProtocolError)
        self.assertIn('fewer bytes than its bitmap marks', str(outcome.receiver_error))
        self.assertFalse(self.output.exists())

    def test_a_patch_beyond_b_is_refused(self):
        original = sender_module._Sender._send_pages

        def beyond(self, job, pages, old=None):
            if job.path != 'base/1/grown':
                return original(self, job, pages, old)
            # Patch one block of page 5, which B (5 pages) doesn't have, as if B held it.
            body = (1).to_bytes(2, 'big') + bytes(512)
            self._send(protocol.PAGE_DELTA, protocol.page_data_head(job.index, 5), body)
            return 512

        with mock.patch.object(sender_module._Sender, '_send_pages', beyond):
            outcome = transfer(self.source, self.basis, self.output, subpage=True)
        self.assertIsInstance(outcome.receiver_error, ProtocolError)
        self.assertIn('does not hold whole', str(outcome.receiver_error))
        self.assertFalse(self.output.exists())

    def test_a_wrong_block_is_caught_by_verification(self):
        original = subpage.encode_page

        def corrupt(page, old, hasher, counts):
            parts = original(page, old, hasher, counts)
            if len(parts) > 1 and len(parts[1]) == 512:  # a partial page: flip a byte of its first block
                parts[1] = bytes([parts[1][0] ^ 1]) + bytes(parts[1][1:])
            return parts

        with mock.patch.object(subpage, 'encode_page', corrupt):
            outcome = transfer(self.source, self.basis, self.output, subpage=True)
        self.assertIsInstance(outcome.receiver_error, VerificationError)
        self.assertFalse(self.output.exists())


SUB_BLOCK_HALF = 256


if __name__ == '__main__':
    unittest.main()
