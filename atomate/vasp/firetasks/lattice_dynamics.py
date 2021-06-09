import json
import os
import subprocess
import shlex
from datetime import datetime
from pathlib import Path

import numpy as np
from joblib import Parallel, delayed

from monty.dev import requires
from monty.serialization import dumpfn, loadfn
from monty.json import jsanitize
from pymongo import ReturnDocument

from atomate.utils.utils import env_chk, get_logger
from atomate.vasp.database import VaspCalcDb
from atomate.vasp.drones import VaspDrone
from atomate.vasp.analysis.lattice_dynamics import (
    FIT_METHOD,
    IMAGINARY_TOL,
    T_QHA,
    T_RENORM,
    T_KLAT,
    fit_force_constants,
    harmonic_properties,
    anharmonic_properties,
    renormalization,
    get_cutoffs
)

from fireworks import FiretaskBase, FWAction, explicit_serialize

from pymatgen.core.structure import Structure
from pymatgen.io.ase import AseAtomsAdaptor
from pymatgen.io.phonopy import get_phonon_band_structure_from_fc, \
    get_phonon_dos_from_fc, get_phonon_band_structure_symm_line_from_fc
from pymatgen.io.shengbte import Control
from pymatgen.transformations.standard_transformations import (
    SupercellTransformation,
)

try:
    import hiphive
    from hiphive import ForceConstants
    from hiphive.utilities import get_displacements
except ImportError:
    logger.info('Could not import hiphive!')
    hiphive = False


__author__ = "Alex Ganose, Junsoo Park"
__email__ = "aganose@lbl.gov, jsyony37@lbl.gov"

logger = get_logger(__name__)

# define shared constants
MESH_DENSITY = 100.0  # should always be a float


@explicit_serialize
class CollectPerturbedStructures(FiretaskBase):
    """
    Aggregate the structures and forces of perturbed supercells.

    Requires a ``perturbed_tasks`` key to be set in the fw_spec, containing a
    list of dictionaries, each with the keys:

    - "parent_structure": The original structure.
    - "supercell_matrix": The supercell transformation matrix.
    - "structure": The perturbed supercell structure.
    - "forces": The forces on each site in the supercell.
    """

    def run_task(self, fw_spec):
        results = fw_spec.get("perturbed_tasks", [])

        if len(results) == 0:
            # can happen if all parents fizzled
            raise RuntimeError("No perturbed tasks found in firework spec")

        logger.info("Found {} perturbed structures".format(len(results)))

        structures = [r["structure"] for r in results]
        forces = [np.asarray(r["forces"]) for r in results]

        # if relaxation, get original structure from that calculation
        calc_locs = fw_spec.get("calc_locs", [])
        opt_calc_locs = [c for c in calc_locs if "optimiz" in c["name"]]
        if opt_calc_locs:
            opt_loc = opt_calc_locs[-1]["path"]
            logger.info("Parsing optimization directory: {}".format(opt_loc))
            opt_doc = VaspDrone(
                parse_dos=False, parse_locpot=False,
                parse_bader=False, store_volumetric_data=[],
            ).assimilate(opt_loc)
            opt_output = opt_doc["calcs_reversed"][0]["output"]
            structure = Structure.from_dict(opt_output["structure"])
        else:
            structure = results[0]["parent_structure"]
        supercell_matrix = results[0]["supercell_matrix"]

        # regenerate pure supercell structure
        st = SupercellTransformation(supercell_matrix)
        supercell_structure = st.apply_transformation(structure)

        structure_data = {
            "structure": structure,
            "supercell_structure": supercell_structure,
            "supercell_matrix": supercell_matrix,
        }

        logger.info("Writing structure and forces data.")
        dumpfn(structures, "perturbed_structures.json")
        dumpfn(forces, "perturbed_forces.json")
        dumpfn(structure_data, "structure_data.json")


@explicit_serialize
class RunHiPhive(FiretaskBase):
    """
    Fit force constants using hiPhive.

    Requires "perturbed_structures.json", "perturbed_forces.json", and
    "structure_data.json" files to be present in the current working directory.

    Optional parameters:
        cutoffs (Optional[list[list]]): A list of cutoffs to trial. If None,
            a set of trial cutoffs will be generated based on the structure
            (default).
        separate_fit: If True, harmonic and anharmonic force constants are fit
            separately and sequentially, harmonic first then anharmonic. If
            False, then they are all fit in one go. Default is False.
        imaginary_tol (float): Tolerance used to decide if a phonon mode
            is imaginary, in THz.
        fit_method (str): Method used for fitting force constants. This can be
            any of the values allowed by the hiphive ``Optimizer`` class.
    """

    optional_params = [
        "cutoffs",
        "separate_fit",
        "bulk_modulus",
        "imaginary_tol",
        "fit_method",
    ]

    @requires(hiphive, "hiphive is required for lattice dynamics workflow")
    def run_task(self, fw_spec):

        imaginary_tol = self.get("imaginary_tol", IMAGINARY_TOL)
        fit_method = self.get("fit_method", FIT_METHOD)
        separate_fit = self.get('separate_fit', False)
        
        all_structures = loadfn("perturbed_structures.json")
        all_forces = loadfn("perturbed_forces.json")
        structure_data = loadfn("structure_data.json")
        parent_structure = structure_data["structure"]
        supercell_structure = structure_data["supercell_structure"]
        supercell_matrix = np.array(structure_data["supercell_matrix"])
        cutoffs = self.get("cutoffs") or get_cutoffs(supercell_structure)
        bulk_modulus = self.get("bulk_modulus")

        structures = []
        supercell_atoms = AseAtomsAdaptor.get_atoms(supercell_structure)
        for structure, forces in zip(all_structures, all_forces):
            atoms = AseAtomsAdaptor.get_atoms(structure)
            displacements = get_displacements(atoms, supercell_atoms)
            atoms.new_array("displacements", displacements)
            atoms.new_array("forces", forces)
            atoms.positions = supercell_atoms.get_positions()
            structures.append(atoms)

        fcs, param, cs, fitting_data = fit_force_constants(
            parent_structure,
            supercell_matrix,
            structures,
            cutoffs,
            separate_fit,
            imaginary_tol,
            fit_method
        )

        if fcs is None:
            # fitting failed for some reason
            raise RuntimeError(
                "Could not find a force constant solution"
            )
        
        thermal_data, phonopy = harmonic_properties(
            parent_structure, supercell_matrix, fcs, T_QHA, imaginary_tol
        )
        anharmonic_data, phonopy = anharmonic_properties(
            phonopy, fcs, T_QHA, thermal_data["heat_capacity"],
            thermal_data["n_imaginary"], bulk_modulus
        )

        fitting_data["n_imaginary"] = thermal_data.pop("n_imaginary")
        thermal_data.update(anharmonic_data)
        dumpfn(fitting_data, "fitting_data.json")
        dumpfn(thermal_data, "thermal_data_qha.json")

        logger.info("Writing cluster space and force_constants")
        logger.info("{}".format(type(fcs)))
        fcs.write("force_constants.fcs")
        np.savetxt('parameters.txt',param)
        cs.write('cluster_space.cs')

        if fitting_data["n_imaginary"] == 0:
            logger.info("No imaginary modes! Writing ShengBTE files")
            atoms = AseAtomsAdaptor.get_atoms(parent_structure)
            fcs.write_to_shengBTE("FORCE_CONSTANTS_3RD", atoms, order=3)
            fcs.write_to_phonopy("FORCE_CONSTANTS_2ND", format="text")
        else:
            logger.info("ShengBTE files not written due to imaginary modes. You may want to perform phonon renormalization.")



@explicit_serialize
class RunHiPhiveRenorm(FiretaskBase):
    """
    Perform phonon renormalization to obtain temperature-dependent force constants
    using hiPhive. Requires "structure_data.json" to be present in the current working
    directory.

    cs,supercell,fcs,param,T,

    Required parameters:
        cs (hiphive.ClusterSpace): A list of cutoffs to trial. If None,
            a set of trial cutoffs will be generated based on the structure
            (default).
        fcs (hiphive.ForceConstants): Maximum number of imaginary modes allowed in the
            the final fitted force constant solution. If this criteria is not
            reached by any cutoff combination this FireTask will fizzle.
        param (np.ndarray): array of force constant parameters
        cutoffs (np.ndarray): cutoffs used for best fitting
   
    Optional parameter:
        T_renorm (List): list of temperatures to perform renormalization - default is T_RENORM
        bulk_modulus (float): manually input bulk modulus - necessary for thermal expansion
        renorm_with_te (bool): if True, 
    """

    required_params = ["cs","fcs","param","cutoff"]
    optional_params = ["T_renorm","bulk_modulus","renorm_with_te","fit_method"]

    @requires(hiphive, "hiphive is required for lattice dynamics workflow")
    def run_task(self, fw_spec):

        imaginary_tol = self.get("imaginary_tol", IMAGINARY_TOL)
        fit_method = self.get("fit_method", FIT_METHOD)

        structure_data = loadfn("structure_data.json")
        parent_structure = structure_data["structure"]
        supercell_structure = structure_data["supercell_structure"]
        supercell_atoms = AseAtomsAdaptor.get_atoms(supercell_structure)
        supercell_matrix = np.array(structure_data["supercell_matrix"])

        cs = self.get("cs")
        fcs = self.get("fcs")
        param = self.get("param")
        bulk_modulus = self.get("bulk_modulus")
        T_renorm = self.get("T_renorm", T_RENORM)

        renorm_results = Parallel(n_jobs=2, backend="multiprocessing")(delayed(renormalization)(
            parent_structure, supercell_matrix, cs, fcs, param, T, nconfig, conv_thresh, imaginary_tol
        ) for t, T in enumerate(T_renorm))

        T_real = []
        cte_real = []
        fcs_real = []
        param_real = []
        for result in renorm_results:
            if result is None:
                continue
            elif result["n_imaginary"] > 0 or bulk_modulus is None:
                fcs = result["fcs"][0]
                fcs.write("force_constants_{}K.fcs".format(T))
                np.savetxt('parameters_{}K.txt'.format(T),result["param"][0])
            else:
                T_real = T_real + result["temperature"]
                cte_real = cte_real + result["thermal_expansion"]
                fcs_real = fcs_real + result["fcs"]
                param_real = param_real + result["param"]
        if len(T_real)==0:
            pass
        elif len(T_real)==1 and T_real[0]==0:
            fcs = result["fcs"][0]
            fcs.write("force_constants_{}K.fcs".format(T))
            np.savetxt('parameters_{}K.txt'.format(T),result["param"][0])
        else:
            ind = np.argsort(np.array(T_real))
            T_real = list(np.array(T_real)[ind])
            cte_real = list(np.array(cte_real)[ind])
            dLfrac_real = thermal_expansion(T_real,cte_real)
            parent_structure_TE, cs_TE, fcs_TE = setup_TE_renorm(parent_structure,T_real,dLfrac_real)
            renorm_results = Parallel(n_jobs=2, backend="multiprocessing")(delayed(renormalization)(
                parent_structure_TE[t], supercell_matrix, cs_TE[t], fcs_TE[t], param, T, nconfig, conv_thresh, imaginary_tol
            ) for t, T in enumerate(T_real))

            logger.info("Writing renormalized results")
            thermal_keys = ["temperature","free_energy","entropy","heat_capacity",
                            "gruneisen","thermal_expansion","expansion_ratio"]
            thermal_data = {key: [] for key in thermal_keys}
            for t, result in enumerate(renorm_results):
                T = result["temperature"][0]
                fcs = result["fcs"][0]
                fcs.write("force_constants_{}K.fcs".format(T))
                np.savetxt('parameters_{}K.txt'.format(T),result["param"][0])
                for key in thermal_keys:
                    thermal_data[key].append(result[key][0])
                if result["n_imaginary"] > 0:
                    logger.warning('Imaginary modes generated for {} K!'.format(T))
                    logger.warning('Renormalization with thermal expansion may have failed')
                    logger.warning('ShengBTE files not written')
                else:
                    logger.info("No imaginary modes! Writing ShengBTE files")
                    fcs.write_to_phonopy("FORCE_CONSTANTS_2ND_{}K".format(T), format="text")

            thermal_data.pop("n_imaginary")                    
            dumpfn(thermal_data, "thermal_data_renorm.json")

        
@explicit_serialize
class ForceConstantsToDb(FiretaskBase):
    """
    Add force constants, phonon band structure and density of states
    to the database.

    Assumes you are in a directory with the force constants, fitting
    data, and structure data written to files.

    Required parameters:
        db_file (str): Path to DB file for the database that contains the
            perturbed structure calculations.

    Optional parameters:
        renormalized (bool): Whether FC resulted from original fitting (False)
            or renormalization process (True) determines how data are stored. 
            Default is False.
        mesh_density (float): The density of the q-point mesh used to calculate
            the phonon density of states. See the docstring for the ``mesh``
            argument in Phonopy.init_mesh() for more details.
        additional_fields (dict): Additional fields added to the document, such
            as user-defined tags, name, ids, etc.
    """

    required_params = ["db_file"]
    optional_params = ["renormalized","mesh_density", "additional_fields"]

    @requires(hiphive, "hiphive is required for lattice dynamics workflow")
    def run_task(self, fw_spec):

        db_file = env_chk(self.get("db_file"), fw_spec)
        mmdb = VaspCalcDb.from_db_file(db_file, admin=True)
        renormalized = self.get("renormalized", False)
        mesh_density = self.get("mesh_density", MESH_DENSITY)

        structure_data = loadfn("structure_data.json")
        forces = loadfn("perturbed_forces.json")
        structures = loadfn("perturbed_structures.json")

        structure = structure_data["structure"]
        supercell_structure = structure_data["supercell_structure"]
        supercell_matrix = structure_data["supercell_matrix"]

        fitting_data = loadfn("fitting_data.json")
        thermal_data = loadfn("thermal_data_qha.json")
        fcs = ForceConstants.read("force_constants.fcs")
        
        dos_fsid, uniform_bs_fsid, lm_bs_fsid, fc_fsid = _get_fc_fsid(
            structure, supercell_matrix, fcs, mesh_density, mmdb
        )
        
        data = {
            "created_at": datetime.utcnow(),            
            "tags": fw_spec.get("tags", None),
            "formula_pretty": structure.composition.reduced_formula,            
            "structure": structure.as_dict(),
            "supercell_matrix": supercell_matrix,
            "supercell_structure": supercell_structure.as_dict(),
            "perturbed_structures": [s.as_dict() for s in structures],
            "perturbed_forces": [f.tolist() for f in forces],
            "fitting_data": fitting_data,
            "thermal_data": thermal_data,
            "force_constants_fs_id": fc_fsid,
            "phonon_dos_fs_id": dos_fsid,
            "phonon_bandstructure_uniform_fs_id": uniform_bs_fsid,
            "phonon_bandstructure_line_fs_id": lm_bs_fsid,
        }
        data.update(self.get("additional_fields", {}))

        # Get an id for the force constants
        fitting_id = _get_fc_fitting_id(mmdb)
        metadata = {"fc_fitting_id": fitting_id, "fc_fitting_dir": os.getcwd()}
        data.update(metadata)
        data = jsanitize(data,strict=True,allow_bson=True)
        
        mmdb.db.lattice_dynamics.insert_one(data)
            
        logger.info("Finished inserting force constants")

        return FWAction(update_spec=metadata)



class ForceConstantsRenormToDb(FiretaskBase):
    """
    Add force constants, phonon band structure and density of states
    to the database.

    Assumes you are in a directory with the force constants, fitting
    data, and structure data written to files.

    Required parameters:
        db_file (str): Path to DB file for the database that contains the
            perturbed structure calculations.
    Optional parameters:
        renormalized (bool): Whether FC resulted from original fitting (False)
            or renormalization process (True) determines how data are stored.
            Default is False.                                                                                                                                                                                      mesh_density (float): The density of the q-point mesh used to calculate
            the phonon density of states. See the docstring for the ``mesh``
            argument in Phonopy.init_mesh() for more details.
        additional_fields (dict): Additional fields added to the document, such
            as user-defined tags, name, ids, etc.                                                                                                                                                              """

    required_params = ["db_file"]
    optional_params = ["renormalized","mesh_density", "additional_fields"]

    @requires(hiphive, "hiphive is required for lattice dynamics workflow")
    def run_task(self, fw_spec):

        db_file = env_chk(self.get("db_file"), fw_spec)
        mmdb = VaspCalcDb.from_db_file(db_file, admin=True)
        renormalized = self.get("renormalized", False)
        mesh_density = self.get("mesh_density", MESH_DENSITY)

        structure_data = loadfn("structure_data.json")
        forces = loadfn("perturbed_forces.json")
        structures = loadfn("perturbed_structures.json")

        structure = structure_data["structure"]
        supercell_structure = structure_data["supercell_structure"]
        supercell_matrix = structure_data["supercell_matrix"]

        thermal_data = loadfn("thermal_data_renorm.json")
        temperature = thermal_data["temperature"]

        data = {
            "created_at": datetime.utcnow(),
            "tags": fw_spec.get("tags", None),
            "formula_pretty": structure.composition.reduced_formula,
            "structure": structure.as_dict(),
            "thermal_data": thermal_data,
            }
        
        temperature_to_keep = []
        renormalized_data = []
        for t, T in enumerate(temperature):
            ## err should I collect results for all T and push data at once,                                                                                                                            
            ## or push data for each T individually?                                                                                                                                                    
            fcs = ForceConstants.read("force_constants_{}K.fcs")
            phonopy_fc = fcs.get_fc_array(order=2)
            dos_fsid, uniform_bs_fsid, lm_bs_fsid, fc_fsid = _get_fc_fsid(
                structure, supercell_matrix, phonopy_fc, mesh_density, mmdb
            )
            
            data_at_T = {
                "force_constants_fs_id": fc_fsid,
                "phonon_dos_fs_id": dos_fsid,
                "phonon_bandstructure_uniform_fs_id": uniform_bs_fsid,
                "phonon_bandstructure_line_fs_id": lm_bs_fsid,
            }
            temperature_to_keep.append(T)
            renormalized_data.append(data_at_T)
        data["temperatures"] = temperature_to_keep
        data["renormalized_data"] = renormalized_data
        data.update(self.get("additional_fields", {}))

        # Get an id for the force constants
        fitting_id = _get_fc_fitting_id(mmdb)
        metadata = {"fc_fitting_id": fitting_id, "fc_fitting_dir": os.getcwd()}
        data.update(metadata)
        data = jsanitize(data,strict=True,allow_bson=True)

        mmdb.db.renormalized_lattice_dynamics.insert_one(data)

        logger.info("Finished inserting renormalized force constants")

        return FWAction(update_spec=metadata)        

    
@explicit_serialize
class RunShengBTE(FiretaskBase):
    """
    Run ShengBTE to calculate lattice thermal conductivity. Presumes
    the FORCE_CONSTANTS_3RD and FORCE_CONSTANTS_2ND, and a "structure_data.json"
    file, with the keys "structure", " and "supercell_matrix" is in the current
    directory.

    Required parameters:
        shengbte_cmd (str): The name of the shengbte executable to run. Supports
            env_chk.

    Optional parameters:
        temperature (float or dict): The temperature to calculate the lattice
            thermal conductivity for. Can be given as a single float, or a
            dictionary with the keys "min", "max", "step".
        control_kwargs (dict): Options to be included in the ShengBTE control
            file.
    """

    required_params = ["shengbte_cmd"]
    optional_params = ["temperature", "control_kwargs"]

    def run_task(self, fw_spec):
        structure_data = loadfn("structure_data.json")
        structure = structure_data["structure"]
        supercell_matrix = structure_data["supercell_matrix"]
        temperature = self.get("temperature", T_KLAT)

        if isinstance(temperature, (int, float)):
            self["t"] = temperature
        elif isinstance(temperature, (list, np.ndarray)):
            t_max = np.max(np.array(temperature))
            t_min = np.min(np.array(temperature))
            if t_min==0:
                t_min = np.partition(temperature,1)[1]
                t_step = (t_max-t_min)/(len(temperature)-2)
            else:
                t_step = (t_max-t_min)/(len(temperature)-1)
            self["t_min"] = t_min
            self["t_max"] = t_max
            self["t_step"] = t_step
        elif isinstance(temperature, dict):
            self["t_min"] = temperature["min"]
            self["t_max"] = temperature["max"]
            self["t_step"] = temperature["step"]
        else:
            raise ValueError("Unsupported temperature type, must be float or dict")
        
        control_dict = {
            "scalebroad": 0.5,
            "nonanalytic": False,
            "isotopes": False,
            "temperature": self.get("temperature", T_KLAT),
            "scell": np.diag(supercell_matrix).tolist(),
        }
        control_kwargs = self.get("control_kwargs") or {}
        control_dict.update(control_kwargs)
        control = Control().from_structure(structure, **control_dict)
        control.to_file()

        shengbte_cmd = env_chk(self["shengbte_cmd"], fw_spec)

        if isinstance(shengbte_cmd, str):
            shengbte_cmd = os.path.expandvars(shengbte_cmd)
            shengbte_cmd = shlex.split(shengbte_cmd)

        shengbte_cmd = list(shengbte_cmd)
        logger.info("Running command: {}".format(shengbte_cmd))

        with open("shengbte.out", "w") as f_std, open(
            "shengbte_err.txt", "w", buffering=1
        ) as f_err:
            # use line buffering for stderr
            return_code = subprocess.call(
                shengbte_cmd, stdout=f_std, stderr=f_err
            )
        logger.info(
            "Command {} finished running with returncode: {}".format(
                shengbte_cmd, return_code
            )
        )

        if return_code == 1:
            raise RuntimeError(
                "Running ShengBTE failed. Check '{}/shengbte_err.txt' for "
                "details.".format(os.getcwd())
            )


@explicit_serialize
class ShengBTEToDb(FiretaskBase):
    """
    Add lattice thermal conductivity results to database.

    Assumes you are in a directory with the ShengBTE results in.

    Required parameters:
        db_file (str): Path to DB file for the database that contains the
            perturbed structure calculations.

    Optional parameters:
        additional_fields (dict): Additional fields added to the document.
    """

    required_params = ["db_file"]
    optional_params = ["additional_fields"]

    def run_task(self, fw_spec):
        db_file = env_chk(self.get("db_file"), fw_spec)
        mmdb = VaspCalcDb.from_db_file(db_file, admin=True)

        control = Control().from_file("CONTROL")
        structure = control.get_structure()
        supercell_matrix = np.diag(control["scell"])

        if Path("BTE.KappaTensorVsT_CONV").exists():
            filename = "BTE.KappaTensorVsT_CONV"
        elif Path("BTE.KappaTensorVsT_RTA").exists():
            filename = "BTE.KappaTensorVsT_RTA"
        else:
            raise RuntimeError("Could not find ShengBTE output files.")

        bte_data = np.loadtxt(filename)
        if len(bte_data.shape) == 1:
            # pad extra axis to make compatible with multiple temperatures
            bte_data = bte_data[None, :]

        temperatures = bte_data[:, 0].tolist()
        kappa = bte_data[:, 1:10].reshape(-1, 3, 3).tolist()

        data = {
            "structure": structure.as_dict(),
            "supercell_matrix": supercell_matrix.tolist(),
            "temperatures": temperatures,
            "lattice_thermal_conductivity": kappa,
            "control": control.as_dict(),
            "tags": fw_spec.get("tags", None),
            "formula_pretty": structure.composition.reduced_formula,
            "shengbte_dir": os.getcwd(),
            "fc_fitting_id": fw_spec.get("fc_fitting_id", None),
            "fc_fitting_dir": fw_spec.get("fc_fitting_dir", None),
            "created_at": datetime.utcnow(),
        }
        data.update(self.get("additional_fields", {}))

        mmdb.collection = mmdb.db["lattice_thermal_conductivity"]
        mmdb.collection.insert(data)


def _get_fc_fitting_id(mmdb: VaspCalcDb) -> int:
    """Helper method to get a force constant fitting id."""
    fc_id = mmdb.db.counter.find_one_and_update(
        {"_id": "fc_fitting_id"},
        {"$inc": {"c": 1}},
        return_document=ReturnDocument.AFTER,
    )
    if fc_id is None:
        mmdb.db.counter.insert({"_id": "fc_fitting_id", "c": 1})
        fc_id = 1
    else:
        fc_id = fc_id["c"]

    return fc_id


def _get_fc_fsid(structure, supercell_matrix, fcs, mesh_density, mmdb):
    phonopy_fc = fcs.get_fc_array(order=2)
    
    logger.info("Getting uniform phonon band structure.")
    uniform_bs = get_phonon_band_structure_from_fc(
        structure, supercell_matrix, phonopy_fc
    )
    
    logger.info("Getting line mode phonon band structure.")
    lm_bs = get_phonon_band_structure_symm_line_from_fc(
        structure, supercell_matrix, phonopy_fc
    )
    
    logger.info("Getting phonon density of states.")
    dos = get_phonon_dos_from_fc(
        structure, supercell_matrix, phonopy_fc, mesh_density=mesh_density
    )
    
    logger.info("Inserting phonon objects into database.")
    dos_fsid, _ = mmdb.insert_gridfs(
        dos.to_json(), collection="phonon_dos_fs"
    )
    uniform_bs_fsid, _ = mmdb.insert_gridfs(
        uniform_bs.to_json(), collection="phonon_bandstructure_fs"
    )
    lm_bs_fsid, _ = mmdb.insert_gridfs(
        lm_bs.to_json(), collection="phonon_bandstructure_fs"
    )
    
    logger.info("Inserting force constants into database.")
    fc_json = json.dumps(
        {str(k): v.tolist() for k, v in fcs.get_fc_dict().items()}
    )
    fc_fsid, _ = mmdb.insert_gridfs(
        fc_json, collection="phonon_force_constants_fs"
    )

    return dos_fsid, uniform_bs_fsid, lm_bs_fsid, fc_fsid