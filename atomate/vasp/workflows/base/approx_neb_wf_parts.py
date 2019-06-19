from fireworks import Firework, FWAction, Workflow, FiretaskBase
from atomate.common.firetasks.glue_tasks import PassCalcLocs
from atomate.vasp.firetasks.glue_tasks import pass_vasp_result, CopyVaspOutputs
from atomate.vasp.firetasks.run_calc import RunVaspCustodian
from atomate.vasp.firetasks.write_inputs import WriteVaspFromIOSet
from atomate.vasp.firetasks.parse_outputs import VaspToDb
from atomate.vasp.fireworks.core import OptimizeFW
from atomate.vasp.config import VASP_CMD, DB_FILE
from pymatgen.io.vasp.sets import MPRelaxSet
from pymatgen import Structure, Element

from atomate.vasp.firetasks.approx_neb_tasks import InsertSites


class InsertSitesFW(Firework):
    # TODO: Write class description
    def __init__(
        self,
        structure,
        insert_specie,
        insert_coords,
        name="approx neb insert working ion",
        vasp_input_set=None,
        override_default_vasp_params=None,
        vasp_cmd=VASP_CMD,
        db_file=DB_FILE,
        parents=None,
        **kwargs
    ):
        override_default_vasp_params = override_default_vasp_params or {}

        # if structure == None and parents == None:
        #   print("ERROR")
        # elif structure == None: #setting structure supercedes parents
        #   connect to database
        #   query for parent using fw_spec['_job_info'][-1]['launch_dir']
        #   get structure...
        #   structure #from parents
        # TODO:Is pass structure needed in this FW? How to ensure pass_dict key matches?
        pass_structure_fw = pass_vasp_result(
            pass_dict={"host_lattice_structure": ">>output.structure"}
        )
        structure = InsertSites(
            insert_specie=insert_specie, insert_coords=insert_coords
        )
        vasp_input_set = vasp_input_set or MPRelaxSet(
            structure, **override_default_vasp_params
        )
        t = [pass_structure_fw]
        t.append(WriteVaspFromIOSet(structure=structure, vasp_input_set=vasp_input_set))
        t.append(RunVaspCustodian(vasp_cmd=vasp_cmd, job_type="double_relaxation_run"))
        t.append(PassCalcLocs(name=name))
        t.append(VaspToDb(db_file=db_file, additional_fields={"task_label": name}))


class PathFinderFW(Firework):
    # TODO: Write PathfinderFW
    # FW requires starting from a previous calc to get CHGCAR
    def __init__(
        self,
        structure,
        insert_specie,
        insert_coords,
        parents=None,
        prev_calc_dir=None,
        name="pathfinder",
        vasp_input_set=None,
        override_default_vasp_params=None,
        vasp_cmd=VASP_CMD,
        db_file=DB_FILE,
        **kwargs
    ):
        override_default_vasp_params = override_default_vasp_params or {}
        t = []
        if prev_calc_dir:
            t.append(
                CopyVaspOutputs(calc_dir=prev_calc_dir, additional_files=["CHGCAR"])
            )
        elif parents:
            t.append(CopyVaspOutputs(calc_loc=True, additional_files=["CHGCAR"]))
        else:
            raise ValueError(
                "Must specify previous calculation to use CHGCAR for PathfinderFW"
            )

        # ToDo: Apply Pathfinder
        task_name = name + "???"
        t.append(RunVaspCustodian(vasp_cmd=vasp_cmd, auto_npar=">>auto_npar<<"))
        t.append(PassCalcLocs(name=task_name))
        t.append(VaspToDb(db_file=db_file, additional_fields={"task_label": task_name}))

    def add_fix_two_atom_selective_dynamics(structure, fixed_index, fixed_specie):
        """
        Returns structure with selective dynamics assigned to fix the
        position of two sites.
        Two sites will be fixed: 1) the site specified by fixed_index and
        2) the site positioned furthest from the specified fixed_index site.

        Args:
            structure (Structure): Input structure (e.g. host lattice with
            one working ion intercalated)
            fixed_index (int): Index of site in structure whose position
            will be fixed (e.g. the working ion site)
            fixed_specie (str or Element): Specie of site in structure
            whose position will be fixed (e.g. the working ion site)
        Returns:
            Structure
        """
        if structure[fixed_index].specie != Element(fixed_specie):
            raise TypeError(
                "The chosen fixed atom at index {} is not a {} atom".format(
                    fixed_index, fixed_specie
                )
            )
        sd_structure = structure.copy()
        sd_array = [[True, True, True] for i in range(sd_structure.num_sites)]
        sd_array[fixed_index] = [False, False, False]
        ref_site = sd_structure.sites[fixed_index]
        distances = [site.distance(ref_site) for site in sd_structure.sites]
        farthest_index = distances.index(max(distances))
        sd_array[farthest_index] = [False, False, False]
        sd_structure.add_site_property("selective_dynamics", sd_array)
        return sd_structure