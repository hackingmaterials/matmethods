import json
import os
import shutil
import unittest
from shutil import which

from pymatgen.io.qchem.outputs import QCOutput
from pymatgen.io.multiwfn import process_multiwfn_qtaim

from atomate.qchem.firetasks.multiwfn import RunMultiwfn_QTAIM
from atomate.utils.testing import AtomateTest

__author__ = "Evan Spotte-Smith"
__email__ = "espottesmith@gmail.com"

module_dir = os.path.dirname(os.path.abspath(__file__))


@unittest.skipIf(not which("Multiwfn_noGUI"), "Multiwfn executable not present")
class TestRunMultiwfn_QTAIM(AtomateTest):

    def setUp(self, lpad=False):
        os.chdir(
            os.path.join(
                module_dir,
                "..",
                "..",
                "test_files",
                "multiwfn_example",
            )
        )
        out_file = "mol.qout.gz"
        qc_out = QCOutput(filename=out_file)
        self.mol = qc_out.data["initial_molecule"]
        self.wavefunction = "WAVEFUNCTION.wfn.gz"
        super().setUp(lpad=False)

    def tearDown(self):
        os.remove("multiwfn_options.txt")
        os.remove("CPprop.txt")

    def test_run(self):
        os.chdir(
            os.path.join(
                module_dir,
                "..",
                "..",
                "test_files",
                "multiwfn_example"
            )
        )
        firetask = RunMultiwfn_QTAIM(
            molecule=self.mol,
            multiwfn_command="Multiwfn_noGUI",
            wfn_file="WAVEFUNCTION.wfn.gz"
        )
        firetask.run_task(fw_spec={})

        reference = process_multiwfn_qtaim(
            self.mol,
            "CPprop_correct.txt"
        )

        this_output = process_multiwfn_qtaim(
            self.mol,
            "CPprop.txt"
        )

        for root in ["atom", "bond", "ring", "cage"]:
            assert len(reference[root]) == len(this_output[root])

            for k, v in reference[root].items():
                assert k in this_output[root]

                for kk, vv in reference[root][k].items():
                    output_val = this_output[root][k].get(kk)
                    if isinstance(vv, list):
                        assert isinstance(output_val, list)
                        assert len(vv) == len(output_val)
                        for index, vvelem in enumerate(vv):
                            self.assertAlmostEqual(vvelem, output_val[index])

                    self.assertAlmostEqual(vv, output_val)
