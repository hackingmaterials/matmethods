# This module defines Firetask that run Multiwfn to analyze a wavefunction (*.wfn) file produced by e.g. Q-Chem.

import os
from pathlib import Path
import subprocess

from fireworks import FiretaskBase, explicit_serialize
from monty.serialization import dumpfn, loadfn
from monty.shutil import compress_file, decompress_file

from atomate.utils.utils import get_logger

__author__ = "Evan Spotte-Smith"
__copyright__ = "Copyright 2024, The Materials Project"
__version__ = "0.1"
__maintainer__ = "Evan Spotte-Smith"
__email__ = "espottesmith@gmail.com"
__status__ = "Alpha"
__date__ = "07/17/2024"


logger = get_logger(__name__)


@explicit_serialize
class RunMultiwfn_QTAIM(FiretaskBase):
    """
    Run the Multiwfn package on an electron density wavefunction (*.wfn) file produced by a Q-Chem calculation
    to generate a CPprop file for quantum theory of atoms in molecules (QTAIM) analysis.

    Required params:
        molecule (Molecule): Molecule object of the molecule whose electron density is being analyzed
                             Note that if prev_calc_molecule is set in the firework spec it will override
                             the molecule required param.
        multiwfn_command (str): Shell command to run Multiwfn
        wfn_file (str): Name of the wavefunction file being analyzed
        output_file (str): Name of the output file containing the Multiwfn outputs
    """

    required_params = ["molecule", "multiwfn_command", "wfn_file"]

    def run_task(self, fw_spec):
        if fw_spec.get("prev_calc_molecule"):
            molecule = fw_spec.get("prev_calc_molecule")
        else:
            molecule = self.get("molecule")
        if molecule is None:
            raise ValueError(
                "No molecule passed and no prev_calc_molecule found in spec! Exiting..."
            )

        compress_at_end = False

        wfn = self.get("wfn_file")

        # File might be compressed
        if not os.path.exists(wfn) and not wfn.endswith(".gz"):
            wfn += ".gz"

        if wfn[-3:] == ".gz":
            compress_at_end = True
            decompress_file(wfn)
            wfn = wfn[:-3]

        # This will run through an interactive Multiwfn dialogue and select the necessary options for QTAIM
        input_script = """
2
2
3
4
5
6
-1
-9
8
7
0
-10
q
        """

        with open("multiwfn_options.txt", "w") as file:
            file.write(input_script)

        cmd = f"{self.get('multiwfn_command')} {wfn} < multiwfn_options.txt"

        logger.info(f"Running command: {cmd}")
        return_code = subprocess.call(cmd, shell=True)
        logger.info(f"Command {cmd} finished running with return code: {return_code}")

        if compress_at_end:
            compress_file(wfn)
