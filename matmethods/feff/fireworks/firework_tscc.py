# coding: utf-8

from __future__ import absolute_import, division, print_function, unicode_literals

"""
Defines standardized Fireworks that can be chained easily to perform various
sequences of FEFF calculations.
"""

from fireworks import Firework

from pymatgen.io.feff.sets import MPEXAFSSet, MPXANESSet, MPELNESSet

from matmethods.feff.firetasks.write_inputs import WriteFeffFromIOSet
from matmethods.feff.db.parse import FEFFDBManager
from matmethods.feff.firetasks.run_calc import RunFeffDirect
from matmethods.feff.firetasks.parse_outputs import AbsorptionSpectrumToDbTask
from matmethods.feff.firetasks.run_calc_tscc import RunFeffTscc

__author__ = 'Kiran Mathew'
__email__ = 'kmathew@lbl.gov'

class FEFFWorkflowManager(object):

    def __init__(self, category=None, debug=False):
        """
        TODO, add function about initializes the Workflow manager using a eels_id. Allows for
        Args:
            category (str): A label for the MD workflow.
            debug (bool): Whether to use the debug DB for testing purposes.
                Defaults to False, i.e., production run.

        To do: might need to add eels id later for tracking structure that has been calculated

        """
        self.category = category
        self.debug = debug

    def _get_spec(self, additional_spec=None):

        spec = {
            "_category": self.category
        }

        if additional_spec:
            spec.update(additional_spec)

        if self.debug:
            spec["debug"] = self.debug

        return spec

    @classmethod
    def from_eels_db(cls,eels_index,settings_file,category=None, debug=False,admin=True):
        """
        TODO add function document
        Args:
            eels_index:
            settings_file:
            category:
            debug:

        Returns:

        """

        feffdb = FEFFDBManager(settings_file,admin)

        feffmanager = FEFFWorkflowManager(category,debug)
        feffmanager.eels_index = eels_index
        feffmanager.settings_file = settings_file
        feffmanager.mp_structures = feffdb.get_mp_id(eels_index)

        return feffmanager


    def create_exafs_workflow(self, absorbing_atom, edge, structure,radius=10,name="EXAFS spectroscopy",feff_input_set=None,override_default_feff_params=None,db_file=None, parents=None, additional_spec=None,**kwargs):

        override_default_feff_params = override_default_feff_params or {}
        feff_input_set = feff_input_set or MPEXAFSSet(absorbing_atom, structure, edge=edge,
                                                      radius=radius, **override_default_feff_params)

        spec = self._get_spec(additional_spec=additional_spec)

        fw = Firework(
            [
                WriteFeffFromIOSet(absorbing_atom=absorbing_atom, structure=structure,
                                    radius=radius, feff_input_set=feff_input_set),
                RunFeffTscc(),
                AbsorptionSpectrumToDbTask(absorbing_atom=absorbing_atom, structure=structure,
                                            db_file=db_file, spectrum_type="EXAFS", output_file="xmu.dat")
            ],
            spec=spec,
            name="{}-{}".format(structure.composition.reduced_formula, name)
        )

        return fw



class EXAFSFW_tscc(Firework):

    def __init__(self, absorbing_atom, structure, edge="K", radius=10.0, name="EXAFS spectroscopy",
                 feff_input_set=None, feff_cmd="feff", override_default_feff_params=None,
                 db_file=None, parents=None, **kwargs):
        """
        Write the input set for FEFF-EXAFS spectroscopy, run feff and insert the absorption
        coefficient to the database('xas' collection).

        Args:
            absorbing_atom (str): absorbing atom symbol
            structure (Structure): input structure
            radius (float): cluster radius in angstroms
            name (str)
            feff_input_set (FeffDictSet)
            feff_cmd (str): path to the feff binary
            override_default_feff_params (dict): override feff tag settings.
            db_file (str): path to the db file.
            parents (Firework): Parents of this particular Firework. FW or list of FWS.
            **kwargs: Other kwargs that are passed to Firework.__init__.
        """
        override_default_feff_params = override_default_feff_params or {}
        feff_input_set = feff_input_set or MPEXAFSSet(absorbing_atom, structure, edge=edge,
                                                      radius=radius, **override_default_feff_params)

        t = []
        t.append(WriteFeffFromIOSet(absorbing_atom=absorbing_atom, structure=structure,
                                    radius=radius, feff_input_set=feff_input_set))
        t.append(RunFeffTscc())
        t.append(AbsorptionSpectrumToDbTask(absorbing_atom=absorbing_atom, structure=structure,
                                            db_file=db_file, spectrum_type="EXAFS", output_file="xmu.dat"))
        super(EXAFSFW_tscc, self).__init__(t, parents=parents, name="{}-{}".
                                         format(structure.composition.reduced_formula, name), **kwargs)