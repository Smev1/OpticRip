import io
import unittest

from opticrip.robot import HumanWriter, RobotWriter, quote


class RobotTests(unittest.TestCase):
    def make(self):
        stream = io.StringIO()
        return RobotWriter(stream), stream

    def test_quote_escapes(self):
        self.assertEqual(quote('a"b\\c'), '"a\\"b\\\\c"')

    def test_message_shape(self):
        writer, stream = self.make()
        writer.message(5014, 1, "C:\\out")
        line = stream.getvalue().strip()
        self.assertTrue(line.startswith('MSG:5014,131072,2,"'))
        # count is the number of parameters, not a severity
        self.assertIn(',"Saving %1 titles into directory %2","1","C:\\\\out"', line)

    def test_zero_parameter_message(self):
        writer, stream = self.make()
        writer.message(5011)
        self.assertEqual(
            stream.getvalue().strip(),
            'MSG:5011,0,0,"Operation successfully completed","Operation successfully completed"',
        )

    def test_drive_line_has_three_strings(self):
        writer, stream = self.make()
        writer.drive(0, 2, 999, 1, "BD-RE HL-DT-ST", "OVER_THE_HEDGE", "D:")
        self.assertEqual(
            stream.getvalue().strip(),
            'DRV:0,2,999,1,"BD-RE HL-DT-ST","OVER_THE_HEDGE","D:"',
        )

    def test_metadata_arities(self):
        writer, stream = self.make()
        writer.cinfo(1, 6206, "DVD disc")
        writer.tinfo(0, 9, 0, "0:03:43")
        writer.sinfo(0, 1, 3, 0, "eng")
        lines = stream.getvalue().strip().splitlines()
        self.assertEqual(lines[0], 'CINFO:1,6206,"DVD disc"')
        self.assertEqual(lines[1], 'TINFO:0,9,0,"0:03:43"')
        self.assertEqual(lines[2], 'SINFO:0,1,3,0,"eng"')

    def test_progress_records(self):
        writer, stream = self.make()
        writer.progress(10, 20)
        writer.action(1, 2, "Saving")
        writer.operation(1, 2, "Opening disc")
        lines = stream.getvalue().strip().splitlines()
        self.assertEqual(lines, ["PRGV:10,20,65536", 'PRGC:1,2,"Saving"', 'PRGT:1,2,"Opening disc"'])

    def test_human_writer_uses_templates(self):
        stream = io.StringIO()
        writer = HumanWriter(stream)
        writer.message(5042)
        self.assertEqual(stream.getvalue().strip(),
                         "The program can't find any usable optical drives.")


if __name__ == "__main__":
    unittest.main()
