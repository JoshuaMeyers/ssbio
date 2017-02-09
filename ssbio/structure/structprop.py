from ssbio.core.object import Object
from ssbio.structure.chainprop import ChainProp
import ssbio.sequence.utils.alignment
import os.path as op
from ssbio.structure.utils.structureio import StructureIO
from cobra.core import DictList
import ssbio.structure.properties
import ssbio.utils
import logging
import numpy as np
import nglview as nv
import seaborn as sns
import ssbio.structure.utils.cleanpdb
log = logging.getLogger(__name__)


class StructProp(Object):
    """Class for protein structural properties"""

    def __init__(self, ident, description=None, chains=None, mapped_chains=None, structure_file=None, file_type=None,
                 reference_seq=None, representative_chain=None, is_experimental=False):
        Object.__init__(self, id=ident, description=description)

        self.is_experimental = is_experimental

        self.reference_seq = reference_seq
        self.reference_seq_top_coverage = 0

        # chains is a DictList of ChainProp objects
        self.chains = DictList()
        if chains:
            self.add_chain_ids(chains)
        # representative_chain is a pointer to the chain in self.chains that matches reference_seq
        self.representative_chain = None
        if representative_chain:
            self.representative_chain = self.chains.get_by_id(representative_chain)
        # mapped_chains is an ordered list of mapped chain IDs which would come from BLAST or the best_structures API
        self.mapped_chains = ssbio.utils.force_list(mapped_chains)

        self.file_type = file_type
        self.structure_path = structure_file
        if structure_file:
            self.load_structure_file(structure_file, file_type)

    @property
    def structure_file(self):
        if not self.structure_path:
            return None
        return op.basename(self.structure_path)

    def load_structure_file(self, structure_file, file_type):
        """Load a structure file and provide pointers to its location

        Args:
            structure_file: Path to structure file
            file_type: Type of structure file
        """
        self.file_type = file_type
        self.structure_path = structure_file

    def parse_structure(self):
        """Read the 3D coordinates of a structure file and return it as a Biopython Structure object

        Also create ChainProp objects in the chains attribute

        Returns:
            Structure: Biopython structure object

        """
        if not self.structure_path:
            log.error('{}: no structure file, unable to parse'.format(self.id))
            return None
        else:
            # Add Biopython structure object
            structure = StructureIO(self.structure_path)

            # Add all chains to self.chains as ChainProp objects
            structure_chains = [x.id for x in structure.first_model.child_list]
            self.add_chain_ids(structure_chains)
            self.get_structure_seqs(structure.first_model)

            # Also add all chains to self.mapped_chains ONLY if there are none specified
            if not self.mapped_chains:
                self.add_mapped_chain_ids(structure_chains)

            return structure

    def clean_structure(self, out_suffix='_clean', outdir=None, force_rerun=False,
                        remove_atom_alt=True, remove_atom_hydrogen=True, keep_atom_alt_id='A', add_atom_occ=True,
                        remove_res_hetero=True, add_chain_id_if_empty='X', keep_chains=None):
        """Clean the structure file associated with this structure, and save it as a new file. Returns the file path.

        Args:
            out_suffix: Suffix to append to original filename
            outdir: Path to output directory
            force_rerun: If structure should be re-cleaned if a clean file exists already
            remove_atom_alt: Remove alternate positions
            remove_atom_hydrogen: Remove hydrogen atoms
            keep_atom_alt_id: If removing alternate positions, which alternate ID to keep
            add_atom_occ: Add atom occupancy fields if not present
            remove_res_hetero: Remove all HETATMs
            add_chain_id_if_empty: Add a chain ID if not present
            keep_chains: Keep only these chains

        Returns:
            str: Path to cleaned PDB file

        """

        if not self.structure_path:
            log.error('{}: no structure file, unable to clean'.format(self.id))
            return None

        clean_pdb_file = ssbio.structure.utils.cleanpdb.clean_pdb(self.structure_path, out_suffix=out_suffix,
                                                                  outdir=outdir, force_rerun=force_rerun,
                                                                  remove_atom_alt=remove_atom_alt,
                                                                  remove_atom_hydrogen=remove_atom_hydrogen,
                                                                  keep_atom_alt_id=keep_atom_alt_id,
                                                                  add_atom_occ=add_atom_occ,
                                                                  remove_res_hetero=remove_res_hetero,
                                                                  add_chain_id_if_empty=add_chain_id_if_empty,
                                                                  keep_chains=keep_chains)

        return clean_pdb_file

    def add_mapped_chain_ids(self, mapped_chains):
        """Add chains by ID into the mapped_chains attribute

        Args:
            mapped_chains (str, list): Chain ID or list of IDs

        """
        mapped_chains = ssbio.utils.force_list(mapped_chains)

        for c in mapped_chains:
            if c not in self.mapped_chains:
                self.mapped_chains.append(c)
                log.debug('{}: added to list of mapped chains'.format(c))
            else:
                log.debug('{}: chain already in list of mapped chains, not adding'.format(c))

    def add_chain_ids(self, chains):
        """Add chains by ID into the chains attribute

        Args:
            chains (str, list): Chain ID or list of IDs

        """
        chains = ssbio.utils.force_list(chains)

        for c in chains:
            if self.chains.has_id(c):
                log.debug('{}: chain already present'.format(c))
            else:
                chain_prop = ChainProp(ident=c, pdb_parent=self.id)
                self.chains.append(chain_prop)
                log.debug('{}: added to chains list'.format(c))

    def get_structure_seqs(self, model):
        """Store chain sequences in the corresponding ChainProp objects in the chains attribute

        Returns:
            DictList: All chain sequences as a DictList of SeqRecords

        """
        # Don't overwrite existing ChainProp objects
        dont_overwrite = []
        chains = list(model.get_chains())
        for x in chains:
            if self.chains.has_id(x.id):
                if self.chains.get_by_id(x.id).seq_record:
                    dont_overwrite.append(x.id)
        if len(dont_overwrite) == len(chains):
            log.debug('Not writing structure sequences, already stored')
            return

        # Returns the structures sequences with Xs added
        structure_seqs = ssbio.structure.properties.residues.get_structure_seqrecords(model)
        log.debug('{}: gathered chain sequences'.format(self.id))

        # Associate with ChainProps
        for seq_record in structure_seqs:
            log.debug('{}: adding chain sequence to ChainProp'.format(seq_record.id))
            my_chain = self.chains.get_by_id(seq_record.id)
            my_chain.seq_record = seq_record

    def align_reference_seq_to_mapped_chains(self, outdir=None, engine='needle', parse=True, force_rerun=False,
                                             **kwargs):
        """Run and store alignments of the reference sequence to chains in mapped_chains.

        Alignments are stored in the reference_seq.structure_alignments attribute.

        Args:
            outdir (str): Directory to output sequence alignment files (only if running with needle)
            engine (str): Which pairwise alignment tool to use ("needle" or "biopython")
            parse (bool): Store locations of mutations, insertions, and deletions in the alignment object (as an annotation)
            force_rerun:
            **kwargs: Other alignment options
        """
        # TODO: **kwargs for alignment options

        if not self.reference_seq:
            raise ValueError('{}: reference sequence not set'.format(self.id))

        # Parse the structure so chain sequences are stored
        my_structure = self.parse_structure()

        for chain_id in self.mapped_chains:
            structure_id = '{}-{}'.format(self.id, chain_id)
            aln_id = '{}_{}'.format(self.reference_seq.id, structure_id)
            outfile = '{}.needle'.format(aln_id)

            if self.reference_seq.structure_alignments.has_id(aln_id):
                log.debug('{}: alignment already completed'.format(chain_id))
                continue

            log.debug('{}: aligning to reference sequence {}'.format(structure_id, self.reference_seq.id))

            chain_prop = self.chains.get_by_id(chain_id)
            chain_seq_record = chain_prop.seq_record
            if not chain_seq_record:
                raise ValueError('{}: chain sequence not parsed'.format(chain_id))

            aln = ssbio.sequence.utils.alignment.pairwise_sequence_alignment(a_seq=self.reference_seq.seq_str,
                                                                             a_seq_id=self.reference_seq.id,
                                                                             b_seq=chain_seq_record,
                                                                             b_seq_id=structure_id,
                                                                             engine=engine,
                                                                             outdir=outdir,
                                                                             outfile=outfile,
                                                                             force_rerun=force_rerun)

            # Add an identifier to the MultipleSeqAlignment object for storage in a DictList
            aln.id = aln_id
            aln.annotations['a_seq'] = self.reference_seq.id
            aln.annotations['b_seq'] = structure_id
            aln.annotations['structure_id'] = self.id
            aln.annotations['chain_id'] = chain_id

            if parse:
                aln_df = ssbio.sequence.utils.alignment.get_alignment_df(a_aln_seq=str(list(aln)[0].seq),
                                                                         b_aln_seq=str(list(aln)[1].seq))
                aln.annotations['mutations'] = ssbio.sequence.utils.alignment.get_mutations(aln_df)
                aln.annotations['deletions'] = ssbio.sequence.utils.alignment.get_deletions(aln_df)
                aln.annotations['insertions'] = ssbio.sequence.utils.alignment.get_insertions(aln_df)

            self.reference_seq.structure_alignments.append(aln)

    def sequence_quality_checker(self, seq_ident_cutoff=0.5, allow_missing_on_termini=0.2,
                                 allow_mutants=True, allow_deletions=False,
                                 allow_insertions=False, allow_unresolved=True):
        """Set the representative chain based on sequence quality checks to the reference sequence.

        Args:
            seq_ident_cutoff:
            allow_missing_on_termini:
            allow_mutants:
            allow_deletions:
            allow_insertions:
            allow_unresolved:

        Returns:

        """
        for alignment in self.reference_seq.structure_alignments:
            chain_id = alignment.annotations['chain_id']

            # Compare representative sequence to structure sequence using the alignment
            found_good_chain = ssbio.structure.properties.quality.sequence_checker(reference_seq_aln=alignment[0],
                                                                        structure_seq_aln=alignment[1],
                                                                        seq_ident_cutoff=seq_ident_cutoff,
                                                                        allow_missing_on_termini=allow_missing_on_termini,
                                                                        allow_mutants=allow_mutants,
                                                                        allow_deletions=allow_deletions,
                                                                        allow_insertions=allow_insertions,
                                                                        allow_unresolved=allow_unresolved)

            # If found_good_pdb = True, set as representative chain
            # If not, move on to the next potential chain
            if found_good_chain:
                self.representative_chain = self.chains.get_by_id(chain_id)
                self.reference_seq_top_coverage = alignment.annotations['percent_identity']
                log.debug('{}: chain {} set as representative'.format(self.id, chain_id))
                return self.representative_chain
        else:
            log.debug('{}: no chains meet quality checks'.format(self.id))
            return None

    def get_dict_with_chain(self, chain, only_keys=None, chain_keys=None, exclude_attributes=None, df_format=False):
        """get_dict method which incorporates attributes found in a specific chain. Does not overwrite any attributes
            in the original StructProp.

        Args:
            chain:
            only_keys:
            chain_keys:
            exclude_attributes:
            df_format:

        Returns:
            dict: attributes of StructProp + the chain specified

        """

        # Choose attributes to return, return everything in the object if a list is not specified
        if not only_keys:
            keys = list(self.__dict__.keys())
        else:
            keys = ssbio.utils.force_list(only_keys)

        # Remove keys you don't want returned
        if exclude_attributes:
            exclude_attributes = ssbio.utils.force_list(exclude_attributes)
            for x in exclude_attributes:
                if x in keys:
                    keys.remove(x)
        else:
            exclude_attributes = []

        exclude_attributes.extend(['mapped_chains', 'chains'])

        final_dict = {k: v for k, v in Object.get_dict(self, only_keys=keys, exclude_attributes=exclude_attributes,
                                                       df_format=df_format).items()}


        chain_prop = self.chains.get_by_id(chain)
        # Filter out keys that show up in StructProp
        if not chain_keys:
            chain_keys = [x for x in chain_prop.get_dict().keys() if x not in final_dict]

        chain_dict = chain_prop.get_dict(only_keys=chain_keys, df_format=df_format)
        final_dict.update(chain_dict)

        return final_dict

    def get_disulfide_bridges(self, threshold=3.0):
        """Run Biopython's search_ss_bonds to find potential disulfide bridges for each chain and store in ChainProp.
        """
        parsed = self.parse_structure()
        if not parsed:
            log.error('{}: unable to open structure to find S-S bridges'.format(self.id))
            return

        disulfide_bridges = ssbio.structure.properties.residues.search_ss_bonds(parsed.first_model,
                                                                                threshold=threshold)
        if not disulfide_bridges:
            log.debug('{}: no disulfide bridges'.format(self.id))

        for chain, bridges in disulfide_bridges.items():
            self.representative_chain.seq_record.annotations['SSBOND-biopython'] = disulfide_bridges[self.representative_chain.id]
            log.debug('{}: found {} disulfide bridges'.format(chain, len(bridges)))
            log.debug('{}: stored disulfide bridges in seq_record letter_annotations'.format(chain))

    def get_residue_depths(self, outdir, force_rerun=False):
        """Run MSMS on this structure and store the residue depths/ca depths in the corresponding ChainProp SeqRecords
        """
        parsed = self.parse_structure()
        if not parsed:
            log.error('{}: unable to open structure to run MSMS'.format(self.id))
            return

        log.debug('{}: running MSMS'.format(self.id))
        msms_results = ssbio.structure.properties.msms.get_msms_df(model=parsed.first_model,
                                                        pdb_file=self.structure_path,
                                                        outdir=outdir, force_rerun=force_rerun)
        if msms_results.empty:
            log.error('{}: unable to run MSMS'.format(self.id))
            return

        chains = msms_results.chain.unique()

        for chain in chains:
            res_depths = msms_results[msms_results.chain == chain].res_depth.tolist()
            ca_depths = msms_results[msms_results.chain == chain].ca_depth.tolist()

            chain_prop = self.chains.get_by_id(chain)
            chain_seq = chain_prop.seq_record

            # Making sure the X's are filled in
            res_depths = ssbio.structure.properties.residues.match_structure_sequence(orig_seq=chain_seq,
                                                                           new_seq=res_depths,
                                                                           fill_with=float('Inf'))

            ca_depths = ssbio.structure.properties.residues.match_structure_sequence(orig_seq=chain_seq,
                                                                          new_seq=ca_depths,
                                                                          fill_with=float('Inf'))

            chain_prop.seq_record.letter_annotations['RES_DEPTH-msms'] = res_depths
            chain_prop.seq_record.letter_annotations['CA_DEPTH-msms'] = ca_depths
            log.debug('{}: stored residue depths in chain seq_record letter_annotations'.format(chain))

    def get_dssp_annotations(self, outdir, force_rerun=False):
        """Run DSSP on this structure and store the DSSP annotations in the corresponding ChainProp SeqRecords

        Args:
            outdir (str): Path to where DSSP dataframe will be stored.
            force_rerun (bool): If DSSP results should be recalculated

        """
        parsed = self.parse_structure()
        if not parsed:
            log.error('{}: unable to open structure to run DSSP'.format(self.id))
            return

        log.debug('{}: running DSSP'.format(self.id))
        dssp_results = ssbio.structure.properties.dssp.get_dssp_df(model=parsed.first_model,
                                                                   pdb_file=self.structure_path,
                                                                   outdir=outdir,
                                                                   force_rerun=force_rerun)

        if dssp_results.empty:
            log.error('{}: unable to run DSSP'.format(self.id))
            return

        chains = dssp_results.chain.unique()
        dssp_summary = ssbio.structure.properties.dssp.secondary_structure_summary(dssp_results)

        for chain in chains:
            ss = dssp_results[dssp_results.chain == chain].ss.tolist()
            exposure_rsa = dssp_results[dssp_results.chain == chain].exposure_rsa.tolist()
            exposure_asa = dssp_results[dssp_results.chain == chain].exposure_asa.tolist()
            phi = dssp_results[dssp_results.chain == chain].phi.tolist()
            psi = dssp_results[dssp_results.chain == chain].psi.tolist()

            chain_prop = self.chains.get_by_id(chain)
            chain_seq = chain_prop.seq_record

            # Making sure the X's are filled in
            ss = ssbio.structure.properties.residues.match_structure_sequence(orig_seq=chain_seq,
                                                                              new_seq=ss,
                                                                              fill_with='-')
            exposure_rsa = ssbio.structure.properties.residues.match_structure_sequence(orig_seq=chain_seq,
                                                                             new_seq=exposure_rsa,
                                                                             fill_with=float('Inf'))
            exposure_asa = ssbio.structure.properties.residues.match_structure_sequence(orig_seq=chain_seq,
                                                                             new_seq=exposure_asa,
                                                                             fill_with=float('Inf'))
            phi = ssbio.structure.properties.residues.match_structure_sequence(orig_seq=chain_seq,
                                                                    new_seq=phi,
                                                                    fill_with=float('Inf'))
            psi = ssbio.structure.properties.residues.match_structure_sequence(orig_seq=chain_seq,
                                                                    new_seq=psi,
                                                                    fill_with=float('Inf'))

            chain_prop.seq_record.annotations.update(dssp_summary[chain])

            chain_prop.seq_record.letter_annotations['SS-dssp'] = ss
            chain_prop.seq_record.letter_annotations['RSA-dssp'] = exposure_rsa
            chain_prop.seq_record.letter_annotations['ASA-dssp'] = exposure_asa
            chain_prop.seq_record.letter_annotations['PHI-dssp'] = phi
            chain_prop.seq_record.letter_annotations['PSI-dssp'] = psi
            log.debug('{}: stored DSSP annotations in chain seq_record letter_annotations'.format(chain))

    def map_repseq_resnums_to_repchain_index(self, resnums):
        """Map a residue number in the reference_seq to an index in the representative_chain

        Use this to get the indices of the repchain to get structural properties at a specific residue number.

        Args:
            resnums (int, list): Residue numbers in the representative sequence

        Returns:
            dict: Mapping of resnums to indices

        """
        resnums = ssbio.utils.force_list(resnums)

        repchain_resnum_mapping = self.reference_seq.seq_record.letter_annotations['repchain_resnums']

        to_repchain_index = {}
        for x in resnums:
            ix = repchain_resnum_mapping[x - 1] - 1

            if np.isnan(ix):
                log.warning('{}, {}: no equivalent residue found in structure sequence'.format(self.id, x))
            else:
                to_repchain_index[x] = int(ix)

        return to_repchain_index

    def map_repseq_resnums_to_structure_resnums(self, resnums):
        """Map a residue number in the reference_seq to the actual structure file's residue number

        Args:
            resnums (int, list): Residue numbers in the representative sequence

        Returns:
            dict: Mapping of resnums to structure residue IDs

        """
        resnums = ssbio.utils.force_list(resnums)

        mapping_to_repchain_index = self.map_repseq_resnums_to_repchain_index(resnums)
        repchain_structure_mapping = self.representative_chain.seq_record.letter_annotations['structure_resnums']

        to_structure_resnums = {}
        for k, v in mapping_to_repchain_index.items():
            rn = repchain_structure_mapping[v]

            if rn[1] == float('Inf'):
                log.warning('{}, {}: structure file does not contain coordinates for this residue'.format(self.id, k))
            else:
                to_structure_resnums[k] = rn

        return to_structure_resnums

    def view_structure(self, opacity=1.0, gui=False):
        """Use NGLviewer to display a structure in a Jupyter notebook

        Args:
            opacity (float): Opacity of the structure
            gui (bool): If the NGLview GUI should show up

        Returns:
            NGLviewer object

        """
        # TODO: test other ways we can manipulate the view object

        if not self.structure_path:
            raise ValueError("Structure file not loaded")
        view = nv.show_structure_file(self.structure_path, gui=gui)
        view.clear_representations()
        view.add_cartoon(selection='protein', color='silver', opacity=opacity)
        return view

    def view_structure_and_highlight_residues(self, structure_resnums, color='red', unique_colors=False,
                                              structure_opacity=0.5, opacity_range=(0.5,1), scale_range=(.7, 10),
                                              gui=False):
        """Input a list of residue numbers to view on the structure. Or input a dictionary of residue numbers to counts
            to scale residues by counts (useful to view mutations).

        Args:
            structure_resnums (int, list, dict): Residue number(s) to highlight, or
                a dictionary of residue number to frequency count
            color (str): Color to highlight with
            unique_colors (bool): If each mutation should be colored uniquely (will override color argument)
            structure_opacity (float): Opacity of the protein structure cartoon representation
            opacity_range (tuple): Min/max opacity values (residues that have higher frequency counts will be opaque)
            scale_range (tuple): Min/max size values (residues that have higher frequency counts will be bigger)
            gui (bool): If the NGLview GUI should show up

        Returns:
            NGLviewer object

        """
        opacity_dict = ssbio.utils.scale_calculator(opacity_range[0], structure_resnums, rescale=opacity_range)
        scale_dict = ssbio.utils.scale_calculator(scale_range[0], structure_resnums, rescale=scale_range)

        view = self.view_structure(opacity=structure_opacity, gui=gui)

        if isinstance(structure_resnums, list):
            unique_mutations = list(set(structure_resnums))
        elif isinstance(structure_resnums, dict):
            unique_mutations = list(structure_resnums.keys())
        elif isinstance(structure_resnums, int):
            unique_mutations = ssbio.utils.force_list(structure_resnums)

        # TODO: add color by letter_annotations!

        colors = sns.color_palette("hls", len(unique_mutations)).as_hex()

        for i, x in enumerate(unique_mutations):
            if isinstance(x, tuple):
                to_show = ''
                for mut in x:
                    to_show += '{} or '.format(mut)
                to_show = to_show.strip(' or ')
            else:
                to_show = x

            if unique_colors:
                view.add_ball_and_stick(selection='not hydrogen and {}'.format(to_show),
                                        color=colors[i], opacity=opacity_dict[x], scale=scale_dict[x])
            else:
                view.add_ball_and_stick(selection='not hydrogen and {}'.format(to_show),
                                        color=color, opacity=opacity_dict[x], scale=scale_dict[x])

        return view

