# coding: utf-8

import os

from atomate.vasp.fireworks.core import OptimizeFW, StaticFW
from fireworks import Workflow, Firework
from atomate.vasp.powerups import add_tags, add_additional_fields_to_taskdocs,\
    add_wf_metadata, add_common_powerups
from atomate.vasp.workflows.base.core import get_wf
from atomate.vasp.firetasks.parse_outputs import MagneticDeformationToDB, MagneticOrderingsToDB

from pymatgen.transformations.advanced_transformations import MagOrderParameterConstraint, \
    MagOrderingTransformation
from pymatgen.analysis.local_env import MinimumDistanceNN

from atomate.utils.utils import get_logger
logger = get_logger(__name__)

from atomate.vasp.config import VASP_CMD, DB_FILE, ADD_WF_METADATA
from uuid import uuid4
from pymatgen.io.vasp.sets import MPRelaxSet
from pymatgen.core import Lattice, Structure
from pymatgen.analysis.magnetism.analyzer import CollinearMagneticStructureAnalyzer, Ordering

__author__ = "Matthew Horton"
__maintainer__ = "Matthew Horton"
__email__ = "mkhorton@lbl.gov"
__status__ = "Development"
__date__ = "March 2017"

__magnetic_deformation_wf_version__ = 1.2
__magnetic_ordering_wf_version__ = 1.3

module_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))


def get_wf_magnetic_deformation(structure,
                                common_params=None,
                                vis=None):
    """
    Minimal workflow to obtain magnetic deformation proxy, as
    defined by Bocarsly et al. 2017, doi: 10.1021/acs.chemmater.6b04729

    :param structure: input structure, must be structure with magnetic
    elements, such that pymatgen will initalize ferromagnetic input by
    default -- see MPRelaxSet.yaml for list of default elements
    :param common_params (dict): Workflow config dict, in the same format
    as in presets/core.py
    :param vis: (VaspInputSet) A VaspInputSet to use for the first FW
    :return:
    """

    if not structure.is_ordered:
        raise ValueError("Please obtain an ordered approximation of the input structure.")

    structure = structure.get_primitive_structure(use_site_props=True)

    # using a uuid for book-keeping,
    # in a similar way to other workflows
    uuid = str(uuid4())

    c = {'vasp_cmd': VASP_CMD, 'db_file': DB_FILE}
    if common_params:
        c.update(common_params)

    wf = get_wf(structure, "magnetic_deformation.yaml",
                common_params=c, vis=vis)

    fw_analysis = Firework(
        MagneticDeformationToDB(
            db_file=DB_FILE,
            wf_uuid=uuid,
            to_db=c.get("to_db", True)),
        name="MagneticDeformationToDB")

    wf.append_wf(Workflow.from_Firework(fw_analysis), wf.leaf_fw_ids)

    wf = add_common_powerups(wf, c)

    if c.get("ADD_WF_METADATA", ADD_WF_METADATA):
        wf = add_wf_metadata(wf, structure)

    wf = add_additional_fields_to_taskdocs(wf, {
        'wf_meta': {
            'wf_uuid': uuid,
            'wf_name': 'magnetic_deformation',
            'wf_version': __magnetic_deformation_wf_version__
        }})

    return wf


def get_wf_magnetic_orderings(structure,
                              vasp_cmd=VASP_CMD,
                              db_file=DB_FILE,
                              default_magmoms=None,
                              respect_input_magmoms="replace_all",
                              calculate_magmoms='VASP', # TODO can be bader -- move to Db instead
                              attempt_ferrimagnetic=None,
                              attempt_afm_by_motif=False,
                              num_orderings=10,
                              max_cell_size=None,
                              vasp_input_set_kwargs=None,
                              perform_bader=True,
                              timeout=None,
                              **kwargs):
    """
    This workflow will try several different collinear
    magnetic orderings for a given input structure,
    and output a summary to a dedicated database
    collection, magnetic_orderings, in the supplied
    db_file.

    If the input structure has magnetic moments defined, it
    is possible to use these as a hint as to which elements are
    magnetic, otherwise magnetic elements will be guessed
    (this can be changed using default_magmoms kwarg).

    A brief description on how this workflow works:
        1. We make a note of the input structure, and then
           sanitize it (make it ferromagnetic, primitive)
        2. We gather information on which sites are likely
           magnetic, how many unique magnetic sites there
           are (whether one species is in several unique
           environments, e.g. tetrahedral/octahedra as Fe
           in a spinel)
        3. We generate ordered magnetic structures, first
           antiferromagnetic, and then, if appropriate,
           ferrimagnetic structures either by species or
           by environment -- this makes use of some new
           additions to MagOrderingTransformation to allow
           the spins of different species to be coupled together
           (e.g. all one species spin up, all other species spin
           down, with an overall order parameter of 0.5)
        4. For each ordered structure, we perform a relaxation
           and static calculation. Then an aggregation is performed
           which finds which ordering is the ground state (of
           those attempted in this specific workflow). For
           high-throughput studies, a dedicated builder is
           recommended.
        5. For book-keeping, a record is kept of whether the
           input structure is enumerated by the algorithm or
           not. This is useful when supplying the workflow with
           a magnetic structure obtained by experiment, to measure
           the performance of the workflow.

    :param structure: input structure
    :param vasp_cmd: as elsewhere in atomate
    :param db_file: as elsewhere in atomate
    :param default_magmoms (dict): dict of magnetic elements
    to their initial magnetic moment in µB
    :param attempt_ferrimagnetic (bool): whether ot not to
    attempt ferrimagnetic structures
    :param num_orderings (int): This is the number of each
    type of ordering to attempt (behind the scenes, it is
    passed to pymatgen's transformation's return_ranked_list).
    Note this is per strategy, so it will return num_orderings
    AFM orderings, but also num_orderings ferrimagnetic by motif,
    etc. if attempt_ferrimagnetic == True
    :param max_cell_size (int): The max_cell_size to consider. If
    too large, enumeration will take a long time! By default will
    try a sensible value (between 4 and 1 depending on number of
    magnetic sites in primitive cell).
    :param perform_bader (bool):
    :param vasp_input_set_kwargs: kwargs to pass to the
    vasp input set, the default is `{'user_incar_settings':
    {'ISYM': 0, 'LASPH': True}`
    :return:
    """

    formula = structure.composition.reduced_formula

    # to process disordered magnetic structures, first make an
    # ordered approximation
    if not structure.is_ordered:
        raise ValueError("Please obtain an ordered approximation of the "
                         "input structure ({}).".format(formula))

    # CollinearMagneticStructureAnalyzer is used throughout:
    # it can tell us whether the input is itself collinear (if not,
    # this workflow is not appropriate), and has many convenience
    # methods e.g. magnetic structure matching, etc.
    input_analyzer = CollinearMagneticStructureAnalyzer(structure,
                                                        default_magmoms=default_magmoms,
                                                        overwrite_magmom_mode="none")

    # this workflow enumerates structures with different combinations
    # of up and down spin and does not include spin-orbit coupling:
    # if your input structure has vector magnetic moments, this
    # workflow is not appropriate
    if not input_analyzer.is_collinear:
        raise ValueError("Input structure ({}) is non-collinear.".format(formula))

    # sanitize input structure: first make primitive ...
    structure = structure.get_primitive_structure(use_site_props=False)
    # ... and strip out existing magmoms, which can cause conflicts
    # with later transformations otherwise since sites would end up
    # with both magmom site properties and Specie spins defined
    if 'magmom' in structure.site_properties:
        structure.remove_site_property('magmom')

    # analyzer is used to obtain information on sanitized input
    analyzer = CollinearMagneticStructureAnalyzer(structure,
                                                  default_magmoms=default_magmoms,
                                                  overwrite_magmom_mode="replace_all")

    # now we can begin to generate our magnetic orderings
    logger.info("Generating magnetic orderings for {}".format(formula))

    mag_species_spin = analyzer.magnetic_species_and_magmoms
    types_mag_species = analyzer.types_of_magnetic_specie
    types_mag_elements = {sp.symbol for sp in types_mag_species}
    num_mag_sites = analyzer.number_of_magnetic_sites
    num_unique_sites = analyzer.number_of_unique_magnetic_sites()

    # enumerations become too slow as number of unique sites (and thus
    # permutations) increase, 8 is a soft limit, this can be increased
    # but do so with care
    if num_unique_sites > 8:
        raise ValueError("Too many magnetic sites to sensibly perform enumeration.")

    # maximum cell size to consider: as a rule of thumb, if the primitive cell
    # contains a large number of magnetic sites, perhaps we only need to enumerate
    # within one cell, whereas on the other extreme if the primitive cell only
    # contains a single magnetic site, we have to create larger supercells
    max_cell_size = max_cell_size if max_cell_size else max(1, int(4/num_mag_sites))
    logger.info("Max cell size set to {}".format(max_cell_size))

    # when enumerating ferrimagnetic structures, it's useful to detect
    # co-ordination numbers on the magnetic sites, since different
    # local environments can result in different magnetic order
    # (e.g. inverse spinels)
    nn = MinimumDistanceNN()
    cns = [nn.get_cn(structure, n) for n in range(len(structure))]
    is_magnetic_sites = [True if site.specie in types_mag_species
                         else False for site in structure]
    # we're not interested in co-ordination numbers for sites
    # that we don't think are magnetic, set these to zero
    cns = [cn if is_magnetic_site else 0
           for cn, is_magnetic_site in zip(cns, is_magnetic_sites)]
    structure.add_site_property('cn', cns)
    unique_cns = set(cns) - {0}

    ### Start generating ordered structures ###

    # if user doesn't specifically request ferrimagnetic orderings,
    # we apply a heuristic as to whether to attempt them or not
    if attempt_ferrimagnetic is None:
        if len(unique_cns) > 1 or len(types_mag_species) > 1:
            attempt_ferrimagnetic = True
        else:
            attempt_ferrimagnetic = False

    # utility function to combine outputs from several transformations
    def _add_structures(ordered_structures, structures_to_add, log_msg=""):
        """
        Transformations with return_ranked_list can return either
        just Structures or dicts (or sometimes lists!) -- until this
        is fixed, we use this function to concat structures given
        by the transformation.
        """
        if structures_to_add:
            # type conversion
            if isinstance(structures_to_add, Structure):
                structures_to_add = [structures_to_add]
            structures_to_add = [s["structure"] if isinstance(s, dict)
                                 else s for s in structures_to_add]
            # concatenation
            ordered_structures += structures_to_add
            logger.info('Adding {} ordered structures{}'.format(len(structures_to_add),
                                                                log_msg))
        return ordered_structures

    # we start with a ferromagnetic ordering
    fm_structure = analyzer.get_ferromagnetic_structure()
    # store magmom as spin property, to be consistent with output from
    # other transformations
    fm_structure.add_spin_by_site(fm_structure.site_properties['magmom'])
    fm_structure.remove_site_property('magmom')

    # we now have our first magnetic ordering...
    ordered_structures = [fm_structure]

    # ...to which we can add simple AFM cases first...
    constraint = MagOrderParameterConstraint(
        0.5,
        # TODO: this list(map(str...)) can probably be removed
        species_constraints=list(map(str, types_mag_species))
    )

    trans = MagOrderingTransformation(mag_species_spin,
                                      order_parameter=[constraint],
                                      max_cell_size=max_cell_size,
                                      timeout=timeout)
    structures_to_add = trans.apply_transformation(structure,
                                                   return_ranked_list=num_orderings)
    ordered_structures = _add_structures(ordered_structures,
                                         structures_to_add,
                                         log_msg=" from antiferromagnetic enumeration")

    # ...and then we also try ferrimagnetic orderings by motif if a
    # single magnetic species is present...
    if attempt_ferrimagnetic and num_unique_sites > 1 and len(types_mag_elements) == 1:

        # these orderings are AFM on one local environment, and FM on the rest
        for cn in unique_cns:
            constraints = [
                MagOrderParameterConstraint(
                    0.5,
                    site_constraint_name='cn',
                    site_constraints=cn
                ),
                MagOrderParameterConstraint(
                    1.0,
                    site_constraint_name='cn',
                    site_constraints=list(unique_cns - {cn})
                )
            ]

            trans = MagOrderingTransformation(mag_species_spin,
                                              order_parameter=constraints,
                                              max_cell_size=max_cell_size,
                                              timeout=timeout)

            structures_to_add = trans.apply_transformation(structure,
                                                           return_ranked_list=num_orderings)

            ordered_structures = _add_structures(ordered_structures,
                                                 structures_to_add,
                                                 log_msg=" from ferrimagnetic motif "
                                                         "enumeration")

    # and also try ferrimagnetic when there are multiple magnetic species
    elif attempt_ferrimagnetic and len(types_mag_species) > 1:

        for sp in types_mag_species:

            constraints = [
                MagOrderParameterConstraint(
                    0.5,
                    species_constraints=str(sp)
                ),
                MagOrderParameterConstraint(
                    1.0,
                    species_constraints=list(map(str, set(types_mag_species) - {sp}))
                )
            ]

            trans = MagOrderingTransformation(mag_species_spin,
                                              order_parameter=constraints,
                                              max_cell_size=max_cell_size,
                                              timeout=timeout)

            structures_to_add = trans.apply_transformation(structure,
                                                           return_ranked_list=num_orderings)

            ordered_structures = _add_structures(ordered_structures,
                                                 structures_to_add,
                                                 log_msg=" from ferrimagnetic species enumeration")

    # ...and finally, we try orderings that are AFM on one local
    # environment, and non-magnetic on the rest -- this is less common
    # but unless explicitly attempted, these states are unlikely to be found
    if attempt_afm_by_motif:
        for cn in unique_cns:
            constraints = [
                MagOrderParameterConstraint(
                    0.5,
                    site_constraint_name='cn',
                    site_constraints=cn
                )
            ]

            trans = MagOrderingTransformation(mag_species_spin,
                                              order_parameter=constraints,
                                              max_cell_size=max_cell_size,
                                              timeout=timeout)

            structures_to_add = trans.apply_transformation(structure,
                                                           return_ranked_list=num_orderings)

            ordered_structures = _add_structures(ordered_structures,
                                                 structures_to_add,
                                                 log_msg=" from antiferromagnetic motif "
                                                         "enumeration")

    # in case we've introduced duplicates, let's remove them
    structures_to_remove = []
    for idx, ordered_structure in enumerate(ordered_structures):
        if idx not in structures_to_remove:
            matches = [ordered_structure.matches(s)
                       for s in ordered_structures]
            structures_to_remove += [match_idx for match_idx, match in enumerate(matches)
                                     if (match and idx != match_idx)]

    if len(structures_to_remove):
        logger.info('Removing {} duplicate ordered structures'.format(len(structures_to_remove)))
        ordered_structures = [s for idx, s in enumerate(ordered_structures)
                              if idx not in structures_to_remove]

    ### Finished generating ordered structures ###

    # Perform book-keeping:
    # indexes keeps track of which ordering we tried first
    # it helps give us statistics for how many orderings we
    # have to try before we get the true expt. ground state (if known)
    indexes = list(range(len(ordered_structures)))

    if input_analyzer.ordering != Ordering.NM:
        # if our input structure isn't in our generated structures,
        # let's add it manually
        matches = [input_analyzer.matches_ordering(s) for s in ordered_structures]
        if not any(matches):
            ordered_structures.append(input_analyzer.structure)
            indexes += [-1]
            logger.info("Input structure not present in enumerated structures, adding...")
            debug_match_index = -1
        else:
            # keep a note of which structure is our input
            # this is mostly for book-keeping
            logger.info("Input structure was found in enumerated "
                        "structures at index {}".format(matches.index(True)))
            debug_match_index = matches.index(True)

    ### Generate FWs for each ordered structure ###

    fws = []
    analysis_parents = []

    for idx, ordered_structure in enumerate(ordered_structures):

        analyzer = CollinearMagneticStructureAnalyzer(ordered_structure)

        name = "ordering {} {} -".format(indexes[idx], analyzer.ordering.value)

        # get keyword arguments for VaspInputSet
        relax_vis_kwargs = {'user_incar_settings': {'ISYM': 0, 'LASPH': True}}
        if vasp_input_set_kwargs:
            relax_vis_kwargs.update(vasp_input_set_kwargs)

        if analyzer.ordering == Ordering.NM:
            # use with care, in general we *do not* want a non-spin-polarized calculation
            # just because initial structure has zero magnetic moments; used here for
            # calculation of magnetic deformation proxy
            relax_vis_kwargs['user_incar_settings'].update({'ISPIN': 1})

        vis = MPRelaxSet(ordered_structure, **relax_vis_kwargs)

        # relax
        fws.append(OptimizeFW(ordered_structure, vasp_input_set=vis,
                              vasp_cmd=vasp_cmd, db_file=db_file,
                              max_force_threshold=0.05,
                              half_kpts_first_relax=False,
                              name=name+" optimize"))

        # static
        fws.append(StaticFW(ordered_structure, vasp_cmd=vasp_cmd,
                            db_file=db_file,
                            name=name+" static",
                            vasp_to_db_kwargs={"perform_bader": True},
                            prev_calc_loc=True, parents=fws[-1]))

        analysis_parents.append(fws[-1])

    uuid = str(uuid4())
    fw_analysis = Firework(MagneticOrderingsToDB(db_file=db_file,
                                                 wf_uuid=uuid,
                                                 auto_generated=False,
                                                 name="MagneticOrderingsToDB",
                                                 parent_structure=structure,
                                                 strategy=vasp_input_set_kwargs,
                                                 perform_bader=perform_bader),
                           name="Magnetic Orderings Analysis", parents=analysis_parents)
    fws.append(fw_analysis)

    wf_name = "{} - magnetic orderings".format(formula)
    wf = Workflow(fws, name=wf_name)

    wf = add_additional_fields_to_taskdocs(wf, {
        'wf_meta': {
            'wf_uuid': uuid,
            'wf_name': 'magnetic_orderings',
            'wf_version': __magnetic_ordering_wf_version__
        }})

    tag = "magnetic_orderings group: >>{}<<".format(uuid)
    wf = add_tags(wf, [tag])

    return wf, debug_match_index, ordered_structures



if __name__ == "__main__":

    # for trying workflow

    from fireworks import LaunchPad

    latt = Lattice.cubic(4.17)
    species = ["Ni", "O"]
    coords = [[0.00000, 0.00000, 0.00000],
              [0.50000, 0.50000, 0.50000]]
    NiO = Structure.from_spacegroup(225, latt, species, coords)

    wf_deformation = get_wf_magnetic_deformation(NiO)

    wf_orderings = get_wf_magnetic_orderings(NiO)

    lpad = LaunchPad.auto_load()
    lpad.add_wf(wf_orderings)
    lpad.add_wf(wf_deformation)
