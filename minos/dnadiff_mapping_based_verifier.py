import logging
import os
import math
import glob

import pyfastaq
import pysam
import pandas as pd

from Bio import pairwise2, SeqIO

from cluster_vcf_records import vcf_clusterer, vcf_file_read

from minos import dependencies, dnadiff, plots, utils

class Error (Exception): pass

class DnadiffMappingBasedVerifier:
    '''dnadiff_snps_file = file of snp calls generated by dnadiff.
    dnadiff_file1 = file containing reference sequence passed to dnadiff.
    dnadiff_file2 = file containing reference sequence passed to dnadiff.
    vcf_file1_in = input VCF file corresponding to dnadiff reference sequence, to be verified.
    vcf_file2_in = input VCF file corresponding to dnadiff query, to be verified.
    vcf_reference_file = reference sequence file that was used to make vcf_file_in.
    outprefix = prefix of output files.
    flank_length = length of "truth" sequence to take before/after alleles when mapping.

    Writes 3 files:
    outprefix.1.sam = mapping of seqs + flanks from dnadiff reference sequence to seqs + flanks for first vcf file.
    outprefix.2.sam = mapping of seqs + flanks from dnadiff query sequence to seqs + flanks for second vcf file.
    outprefix.vcf = VCF file with annotations for validation
    outprefix.stats.tsv = summary stats (see dict output by
                          _parse_sam_file_and_update_vcf_records_and_gather_stats()
                          for a description)'''
    def __init__(self, dnadiff_snps_file, dnadiff_file1, dnadiff_file2, vcf_file_in1, vcf_file_in2, vcf_reference_file, outprefix, flank_length=31, merge_length=None, filter_and_cluster_vcf=True, discard_ref_calls=True, allow_flank_mismatches=True, exclude_regions_bed_file1=None, exclude_regions_bed_file2=None, max_soft_clipped=3):
        self.dnadiff_snps_file = os.path.abspath(dnadiff_snps_file)
        self.dnadiff_file1 = os.path.abspath(dnadiff_file1)
        self.dnadiff_file2 = os.path.abspath(dnadiff_file2)
        self.vcf_file_in1 = os.path.abspath(vcf_file_in1)
        self.vcf_file_in2 = os.path.abspath(vcf_file_in2)
        self.vcf_reference_file = os.path.abspath(vcf_reference_file)
        self.sam_file_out1 = os.path.abspath(outprefix + '.1.sam')
        self.sam_file_out2 = os.path.abspath(outprefix + '.2.sam')
        self.seqs_out_dnadiff1 = os.path.abspath(outprefix + '.dnadiff1.fa')
        self.seqs_out_dnadiff2 = os.path.abspath(outprefix + '.dnadiff2.fa')
        self.filtered_vcf1 = os.path.abspath(outprefix + '.1.filter.vcf')
        self.filtered_vcf2 = os.path.abspath(outprefix + '.2.filter.vcf')
        self.clustered_vcf1 = os.path.abspath(outprefix + '.1.filter.cluster.vcf')
        self.clustered_vcf2 = os.path.abspath(outprefix + '.2.filter.cluster.vcf')
        self.seqs_out_vcf1 = os.path.abspath(outprefix + '.vcf1.fa')
        self.seqs_out_vcf2 = os.path.abspath(outprefix + '.vcf2.fa')
        self.sam_summary = os.path.abspath(outprefix + '.summary.tsv')
        self.stats_out = os.path.abspath(outprefix + '.stats.tsv')
        self.gt_conf_hist_out = os.path.abspath(outprefix + '.gt_conf_hist.tsv')

        self.flank_length = flank_length
        self.merge_length = flank_length if merge_length is None else merge_length
        self.filter_and_cluster_vcf = filter_and_cluster_vcf
        self.discard_ref_calls = discard_ref_calls
        self.allow_flank_mismatches = allow_flank_mismatches

        if self.filter_and_cluster_vcf:
            self.vcf_to_check1 = self.clustered_vcf1
            self.vcf_to_check2 = self.clustered_vcf2
        else:
            self.vcf_to_check1 = self.vcf_file_in1
            self.vcf_to_check2 = self.vcf_file_in2

        self.exclude_regions1 = DnadiffMappingBasedVerifier._load_exclude_regions_bed_file(exclude_regions_bed_file1)
        self.exclude_regions2 = DnadiffMappingBasedVerifier._load_exclude_regions_bed_file(exclude_regions_bed_file2)
        self.max_soft_clipped = max_soft_clipped
        self.number_ns = 1

    @classmethod
    def _write_dnadiff_plus_flanks_to_fastas(cls, dnadiff_file, ref_infile, query_infile, ref_outfile, query_outfile, flank_length):
        '''Given a dnadiff snps file and the corresponding ref and query fasta,
        write a fasta for each infile containing each variant plus flank_length
        nucleotides added to its start and end.
        Calls each sequence:
            dnadiff_snps_index.start_position.'''
        seq1 = ""
        with open(ref_infile, "r") as in_handle1:
            for record in SeqIO.parse(in_handle1, "fasta"):
                seq1 = str(record.seq)
                break

        seq2 = ""
        with open(query_infile, "r") as in_handle2:
            for record in SeqIO.parse(in_handle2, "fasta"):
                seq2 = str(record.seq)
                break

        out_handle1 = open(ref_outfile, "w")
        out_handle2 = open(query_outfile, "w")

        snps = pd.read_csv(dnadiff_file, sep='\t', header=None)

        for line in snps.itertuples():
            assert(len(line) > 4)
            seq_name = str(line[0]) + "." + str(line[1])
            flanked_seq = ""
            if line[2] == '.':
                start = max(0,line[1] - flank_length)
                end = min(line[1] + flank_length, len(seq1))
                flanked_seq = seq1[start:end]
            else:
                start = max(0, line[1] - flank_length - 1)
                end = min(line[1] + flank_length, len(seq1))
                flanked_seq = seq1[start:end]
            print('>' + seq_name, flanked_seq, sep='\n', file=out_handle1)
            if line[3] == '.':
                start = max(0, line[4] - flank_length)
                end = min(line[4] + flank_length, len(seq2))
                flanked_seq = seq2[start:end]
            else:
                start = max(0, line[4] - flank_length - 1)
                end = min(line[4] + flank_length, len(seq2))
                flanked_seq = seq2[start:end]
            print('>' + seq_name, flanked_seq, sep='\n', file=out_handle2)

        out_handle1.close()
        out_handle2.close()

    @classmethod
    def _load_exclude_regions_bed_file(cls, infile):
        regions = {}
        if infile is not None:
            with open(infile) as f:
                for line in f:
                    fields = line.rstrip().split('\t')
                    if fields[0] not in regions:
                        regions[fields[0]] = []
                    start = int(fields[1])
                    end = int(fields[2]) - 1
                    regions[fields[0]].append(pyfastaq.intervals.Interval(start, end))

            for ref_name in regions:
                pyfastaq.intervals.merge_overlapping_in_list(regions[ref_name])

        return regions

    @classmethod
    def _interval_intersects_an_interval_in_list(cls, interval, interval_list):
        # This could be faster by doing something like a binary search.
        # But we're looking for points in intervals, so fiddly to implement.
        # Not expecting a log interval list, so just do a simple check
        # from start to end for now
        i = 0
        while i < len(interval_list) and interval.start > interval_list[i].end:
            i += 1

        return i < len(interval_list) and interval.intersects(interval_list[i])

    @classmethod
    def _filter_vcf_for_clustering(cls, infile, outfile, discard_ref_calls=True):
        header_lines, vcf_records = vcf_file_read.vcf_file_to_dict(infile, sort=True, homozygous_only=False, remove_asterisk_alts=True, remove_useless_start_nucleotides=True)

        with open(outfile, 'w') as f:
            print(*header_lines, sep='\n', file=f)
            for ref_name in vcf_records:
                for vcf_record in vcf_records[ref_name]:
                    if vcf_record.FILTER == 'MISMAPPED_UNPLACEABLE':
                        continue
                    if vcf_record.FORMAT is None or 'GT' not in vcf_record.FORMAT:
                        logging.warning('No GT in vcf record:' + str(vcf_record))
                        continue
                    if vcf_record.REF in [".", ""]:
                        continue

                    genotype = vcf_record.FORMAT['GT']
                    genotypes = genotype.split('/')
                    called_alleles = set(genotypes)
                    if len(called_alleles) != 1 or (discard_ref_calls and called_alleles == {'0'}) or '.' in called_alleles:
                        continue

                    if len(vcf_record.ALT) > 1:
                        if called_alleles != {'0'}:
                            vcf_record.set_format_key_value('GT', '1/1')
                            try:
                                vcf_record.ALT = [vcf_record.ALT[int(genotypes[0]) - 1]]
                            except:
                                raise Error('BAD VCf line:' + str(vcf_record))
                        else:
                            vcf_record.set_format_key_value('GT', '0/0')
                            vcf_record.ALT = [vcf_record.ALT[0]]
                    if vcf_record.ALT[0] in [".",""]:
                        continue

                    if vcf_record.FORMAT['GT'] == '0':
                        vcf_record.FORMAT['GT'] = '0/0'
                    elif vcf_record.FORMAT['GT'] == '1':
                        vcf_record.FORMAT['GT'] = '1/1'

                    if 'GL' in vcf_record.FORMAT.keys() and 'GT_CONF' not in vcf_record.FORMAT.keys():
                        likelihoods = vcf_record.FORMAT['GL'].split(',')
                        assert(len(likelihoods) > 2)
                        if called_alleles == {'0'}:
                            vcf_record.set_format_key_value('GT_CONF',str(float(likelihoods[0]) - float(likelihoods[1])))
                        else:
                            vcf_record.set_format_key_value('GT_CONF', str(float(likelihoods[int(genotypes[0])]) - float(likelihoods[0])))
                    if 'SupportFraction' in vcf_record.INFO.keys() and 'GT_CONF' not in vcf_record.FORMAT.keys():
                        vcf_record.set_format_key_value('GT_CONF',
                                                        str(float(vcf_record.INFO['SupportFraction'])*100))
                    print(vcf_record, file=f)


    @classmethod
    def _write_vars_plus_flanks_to_fasta(cls, outfile, vcf_records, ref_seqs, flank_length, number_ns=0):
        '''Given a dict of vcf records made by vcf_file_read.vcf_file_to_dict(),
        and its correcsponding file of reference sequences, writes a new fasta file
        of each ref seq and inferred variant sequence plus flank_length nucleotides added to
        its start and end. Calls each sequence:
            ref_name.start_position.vcf_list_index.allele_number
        where allele_numbers in same order as VCF, with ref seq = allele 0.'''
        prev_ref_name = None
        prev_ref_pos = None
        j = 0
        with open(outfile, 'w') as f:
            for ref_name in sorted(vcf_records):
                for i, vcf_record in enumerate(vcf_records[ref_name]):
                    start_position, alleles = vcf_record.inferred_var_seqs_plus_flanks(ref_seqs[ref_name], flank_length)

                    for allele_index, allele_seq in enumerate(alleles):
                        seq_name = '.'.join([ref_name, str(start_position + 1), str(j), str(i), str(allele_index)])
                        allele_seq = allele_seq.replace('.','')
                        for n in range(number_ns):
                            allele_seq = "N" + allele_seq + "N"
                        print('>' + seq_name, allele_seq, sep='\n', file=f)
                    if prev_ref_name == ref_name and prev_ref_pos == start_position:
                        j += 1
                    else:
                        j = 0
                    prev_ref_name = ref_name
                    prev_ref_pos = start_position



    @classmethod
    def _map_seqs_to_seqs(cls, seqs_file_ref, seqs_file_query, outfile):
        '''Map seqs_file to ref_file using BWA MEM.
        Output is SAM file written to outfile'''
        bwa_binary = dependencies.find_binary('bwa')
        command = ' '.join([
            bwa_binary, 'index',
            seqs_file_ref,
        ])
        utils.syscall(command)

        command = ' '.join([
            bwa_binary, 'aln',
            seqs_file_ref,
            seqs_file_query,
            '>', outfile + ".tmp",
        ])
        utils.syscall(command)

        command = ' '.join([
            bwa_binary, 'samse',
            seqs_file_ref,
            outfile + ".tmp",
            seqs_file_query,
            '>', outfile,
        ])
        utils.syscall(command)
        #os.unlink(outfile + ".tmp")

    @classmethod
    def _check_if_sam_match_is_good(cls, sam_record, ref_seqs, flank_length, query_sequence=None, allow_mismatches=True, max_soft_clipped=3):
        logging.debug(f'Checking SAM: {sam_record}')
        if sam_record.is_unmapped:
            return 'Unmapped'

        if not allow_mismatches:
            try:
                nm = sam_record.get_tag('NM')
            except:
                raise Error('No NM tag found in sam record:' + str(sam_record))

            all_mapped = len(sam_record.cigartuples) == 1 and sam_record.cigartuples[0][0] == 0
            if all_mapped and nm == 0:
                logging.debug('SAM record passed no mismatches allowed check')
                return 'Good'
            else:
                logging.debug('SAM record failed no mismatches allowed check')
                return 'Bad_mismatches'

        # don't allow too many soft clipped bases
        if (sam_record.cigartuples[0][0] == 4 and sam_record.cigartuples[0][1] > max_soft_clipped) \
                or (sam_record.cigartuples[-1][0] == 4 and sam_record.cigartuples[-1][1] > max_soft_clipped):
            logging.debug('SAM record failed soft clipping check')
            return 'Bad_soft_clipped'

        if query_sequence is None:
            query_sequence = sam_record.query_sequence
        assert query_sequence is not None

        # if the query is short, which happens when the variant we
        # are checking is too near the start or end of the ref sequence
        if len(query_sequence) < 2 * flank_length + 1:
            # This is an edge case. We don't really know which part
            # of the query seq we're looking for, so guess
            length_diff = 2 * flank_length - len(query_sequence)

            if sam_record.query_alignment_start < 5:
                alt_seq_end = len(query_sequence) - flank_length - 1
                alt_seq_start = min(alt_seq_end, flank_length - length_diff)
            else:
                alt_seq_start = flank_length
                alt_seq_end = max(alt_seq_start, length_diff + len(query_sequence) - flank_length - 1)
        else:
            alt_seq_start = flank_length
            alt_seq_end = len(query_sequence) - flank_length - 1

        aligned_pairs = sam_record.get_aligned_pairs(with_seq=True)
        logging.debug(f'aligned_pairs: {aligned_pairs}')
        wanted_aligned_pairs = []
        current_pos = 0

        i = 0
        while i < len(query_sequence):
            if aligned_pairs[i][0] is None:
                if alt_seq_start - 1 <= current_pos <= alt_seq_end + 1:
                    wanted_aligned_pairs.append(aligned_pairs[i])
            elif current_pos > alt_seq_end:
                break
            else:
                current_pos = aligned_pairs[i][0]
                if alt_seq_start - 1 <= current_pos <= alt_seq_end + 1:
                    wanted_aligned_pairs.append(aligned_pairs[i])

            i += 1

        logging.debug(f'wanted_aligned_pairs: {wanted_aligned_pairs}')
        assert len(wanted_aligned_pairs) > 0

        for pair in wanted_aligned_pairs:
            if None in pair or query_sequence[pair[0]].upper() != pair[2].upper():
                logging.debug('SAM record failed because mismatch in allele sequence plus 1bp either side')
                return 'Bad_allele_mismatch'

        logging.debug('SAM record passed all checks')
        return 'Good'

    @classmethod
    def _index_vcf(cls, vcffile):
        '''Index VCF file'''
        bgzip_binary = dependencies.find_binary('bgzip')
        command = ' '.join([
            bgzip_binary,
            '-c',
            vcffile,
            '>',
            vcffile + ".gz",
        ])
        utils.syscall(command)

        tabix_binary = dependencies.find_binary('tabix')
        command = ' '.join([
            tabix_binary,
            '-p',
            'vcf',
            vcffile + ".gz",
        ])
        utils.syscall(command)

    @classmethod
    def _parse_sam_file_and_vcf(cls, samfile, vcffile, dnadiff_plus_flanks_file, flank_length, allow_mismatches, exclude_regions=None, max_soft_clipped=3, number_ns=0):
        if  exclude_regions is None:
            exclude_regions = {}

        found = []
        match_flag = []
        correct_allele = []
        gt_conf = []
        allele = []
        dnadiff_file_seqs = {}
        pyfastaq.tasks.file_to_dict(dnadiff_plus_flanks_file, dnadiff_file_seqs)
        samfile_handle = pysam.AlignmentFile(samfile, "r")
        sam_previous_record_name = None
        num_records = 0
        for sam_record in samfile_handle.fetch(until_eof=True):
            num_records += 1
            if sam_record.query_name == sam_previous_record_name:
                logging.debug(f'SAM record {sam_record.query_name} matches {sam_previous_record_name} so skip')
                continue
            sam_previous_record_name = sam_record.query_name
            found_conf = False
            found_allele = False

            # see if excluded region in bed file
            var_num, start = sam_record.query_name.rsplit('.', maxsplit=2)
            exclude = False
            for ref_name in exclude_regions.keys():
                end = int(start) + 1
                interval = pyfastaq.intervals.Interval(start, end)
                exclude = DnadiffMappingBasedVerifier._interval_intersects_an_interval_in_list(interval,
                                                                                        exclude_regions[ref_name])
            if exclude:
                found.append('Exclude')
                gt_conf.append(0)
                allele.append('0')
                match_flag.append('Exclude')
                correct_allele.append('0')
                continue

            match = DnadiffMappingBasedVerifier._check_if_sam_match_is_good(sam_record,
                                                                                 dnadiff_file_seqs,
                                                                                 flank_length,
                                                                                 query_sequence=sam_record.query_sequence,
                                                                                 allow_mismatches=allow_mismatches,
                                                                                 max_soft_clipped=max_soft_clipped)
            alignment_start = str(sam_record).split("\t")[3]
            match_flag.append(match)
            if match == 'Good':
                logging.debug('SAM record is a good match')
                logging.debug('SAM record reference is %s' %sam_record.reference_name)
                ref_name, expected_start, vcf_pos_index, vcf_record_index, allele_index = sam_record.reference_name.rsplit('.', maxsplit=4)

                vcf_reader = pysam.VariantFile(vcffile)
                vcf_interval_start = int(expected_start) + int(alignment_start) + flank_length - 2 - number_ns
                vcf_interval_end = int(expected_start) + int(alignment_start) + flank_length - number_ns
                logging.debug('Find VCF records matching ref %s in interval [%i,%i]' % (ref_name, vcf_interval_start, vcf_interval_end))
                for i, vcf_record in enumerate(vcf_reader.fetch(ref_name, vcf_interval_start, vcf_interval_end)):
                    if i == int(vcf_pos_index):
                        sample_name = vcf_record.samples.keys()[0]
                        if 'GT' in vcf_record.format.keys() and len(set(vcf_record.samples[sample_name]['GT'])) == 1:
                            if int(allele_index) == vcf_record.samples[sample_name]['GT'][0]:
                                found.append('1')
                                allele.append(str(allele_index))
                                correct_allele.append('1')
                                found_allele = True
                                if 'GT_CONF' in vcf_record.format.keys():
                                    gt_conf.append(int(float(vcf_record.samples[sample_name]['GT_CONF'])))
                                    found_conf = True
            if not found_allele:
                found.append('0')
                allele.append('0')
                correct_allele.append('0')
            if not found_conf:
                gt_conf.append(0)
        logging.debug(f'Length of found: {len(found)}, Length of gt_conf: {len(gt_conf)}, Length of allele: {len(allele)}, Length of match_flag: {len(match_flag)}, Length of correct_allele: {len(correct_allele)}')
        assert len(found) == len(gt_conf)
        assert len(found) == len(allele)
        assert len(found) == len(match_flag)
        assert len(found) == len(correct_allele)
        logging.debug(f'Found a total of {num_records} SAM records')
        return found, gt_conf, allele, match_flag, correct_allele

    @classmethod
    def _parse_sam_files(cls, dnadiff_file, samfile1, samfile2, vcffile1, vcffile2, reffasta1, reffasta2, outfile, flank_length, allow_mismatches=True, exclude_regions1=None, exclude_regions2=None, max_soft_clipped=3, number_ns=0):
        '''Input is the original dnadiff snps file of sites we are searching for
        and 2 SAM files made by _map_seqs_to_seqs(), which show mappings of snp sites
        from from the dnadiff snps file to the vcf (i.e. searches if VCF contains an record
        with the appropriate sequence.
        Creates a tsv detailing whether the snp difference could be detected and at what
        GT_CONF threshold.
        '''

        snps = pd.read_csv(dnadiff_file, sep='\t', header=None)
        ref_found, ref_conf, ref_allele, ref_match_flag, ref_allele_flag = DnadiffMappingBasedVerifier._parse_sam_file_and_vcf(samfile1, vcffile1, reffasta1, flank_length, allow_mismatches, exclude_regions1, max_soft_clipped, number_ns)
        query_found, query_conf, query_allele, query_match_flag, query_allele_flag = DnadiffMappingBasedVerifier._parse_sam_file_and_vcf(samfile2, vcffile2, reffasta2, flank_length, allow_mismatches, exclude_regions2, max_soft_clipped, number_ns)
        logging.debug(f'Length of SNPs to verify: {len(snps[0])}')
        logging.debug(f'Length of ref info found: {len(ref_found)}')
        logging.debug(f'Length of query info found: {len(query_found)}')
        assert len(snps[0]) == len(ref_found)
        assert len(snps[0]) == len(query_found)
        out_df = pd.DataFrame({'id': snps[0],
                               'ref': snps[1],
                               'alt': snps[2],
                               'ref_found': ref_found,
                               'ref_conf' : ref_conf,
                               'ref_allele': ref_allele,
                               'ref_match_flag': ref_match_flag,
                               'ref_allele_correct': ref_allele_flag,
                               'query_found': query_found,
                               'query_conf': query_conf,
                               'query_allele': query_allele,
                               'query_match_flag': query_match_flag,
                               'query_allele_correct': query_allele_flag
                               })
        out_df.to_csv(outfile, sep='\t')

    @classmethod
    def _gather_stats(cls, tsv_file):
        stats = {x: 0 for x in ['total', 'found_vars', 'missed_vars', 'excluded_vars']}
        gt_conf_hist = {}

        snps = pd.read_csv(tsv_file, sep='\t', index_col=0)
        for index,line in snps.iterrows():
            stats['total'] += 1
            if (line['ref_found'] == 'Exclude' or line['query_found'] == 'Exclude'):
                stats['excluded_vars'] += 1
            elif (line['ref_found'] == 1 or line['query_found'] == 1 or line['ref_found'] == '1' or line['query_found'] == '1'):
                stats['found_vars'] += 1
                gt_confs = [i for i in {line['ref_conf'],line['query_conf']} if not math.isnan(i)]
                gt_conf = None
                if len(gt_confs) > 0:
                    gt_conf = max(gt_confs)
                gt_conf_hist[gt_conf] = gt_conf_hist.get(gt_conf, 0) + 1
            else:
                stats['missed_vars'] += 1
        return stats, gt_conf_hist

    def run(self):
        # Write files of sequences to search for in each vcf
        DnadiffMappingBasedVerifier._write_dnadiff_plus_flanks_to_fastas(self.dnadiff_snps_file, self.dnadiff_file1, self.dnadiff_file2, self.seqs_out_dnadiff1, self.seqs_out_dnadiff2, self.flank_length)

        # Cluster together variants in each vcf
        if self.filter_and_cluster_vcf:
            DnadiffMappingBasedVerifier._filter_vcf_for_clustering(self.vcf_file_in1, self.filtered_vcf1, self.discard_ref_calls)
            DnadiffMappingBasedVerifier._filter_vcf_for_clustering(self.vcf_file_in2, self.filtered_vcf2, self.discard_ref_calls)
            if self.discard_ref_calls:
                clusterer1 = vcf_clusterer.VcfClusterer([self.filtered_vcf1], self.vcf_reference_file, self.clustered_vcf1, merge_method='simple', max_distance_between_variants=self.merge_length)
                clusterer2 = vcf_clusterer.VcfClusterer([self.filtered_vcf2], self.vcf_reference_file, self.clustered_vcf2, merge_method='simple', max_distance_between_variants=self.merge_length)
            else:
                clusterer1 = vcf_clusterer.VcfClusterer([self.filtered_vcf1], self.vcf_reference_file, self.clustered_vcf1, merge_method='gt_aware', max_distance_between_variants=self.merge_length)
                clusterer2 = vcf_clusterer.VcfClusterer([self.filtered_vcf2], self.vcf_reference_file, self.clustered_vcf2, merge_method='gt_aware', max_distance_between_variants=self.merge_length)
            clusterer1.run()
            clusterer2.run()

        vcf_header, vcf_records1 = vcf_file_read.vcf_file_to_dict(self.vcf_to_check1, sort=True, remove_useless_start_nucleotides=True)
        vcf_header, vcf_records2 = vcf_file_read.vcf_file_to_dict(self.vcf_to_check2, sort=True, remove_useless_start_nucleotides=True)
        sample_from_header = vcf_file_read.get_sample_name_from_vcf_header_lines(vcf_header)
        if sample_from_header is None:
            sample_from_header = 'sample'
        vcf_ref_seqs = {}
        pyfastaq.tasks.file_to_dict(self.vcf_reference_file, vcf_ref_seqs)

        DnadiffMappingBasedVerifier._write_vars_plus_flanks_to_fasta(self.seqs_out_vcf1, vcf_records1, vcf_ref_seqs, self.flank_length, self.number_ns)
        DnadiffMappingBasedVerifier._write_vars_plus_flanks_to_fasta(self.seqs_out_vcf2, vcf_records2, vcf_ref_seqs, self.flank_length, self.number_ns)
        DnadiffMappingBasedVerifier._map_seqs_to_seqs(self.seqs_out_vcf1, self.seqs_out_dnadiff1, self.sam_file_out1)
        DnadiffMappingBasedVerifier._map_seqs_to_seqs(self.seqs_out_vcf2, self.seqs_out_dnadiff2, self.sam_file_out2)
        #for f in glob.glob(self.seqs_out_vcf1 + '*'):
            #os.unlink(f)
        #for f in glob.glob(self.seqs_out_vcf2 + '*'):
            #os.unlink(f)

        DnadiffMappingBasedVerifier._index_vcf(self.vcf_to_check1)
        self.vcf_to_check1 = self.vcf_to_check1 + ".gz"
        DnadiffMappingBasedVerifier._index_vcf(self.vcf_to_check2)
        self.vcf_to_check2 = self.vcf_to_check2 + ".gz"
        DnadiffMappingBasedVerifier._parse_sam_files(self.dnadiff_snps_file, self.sam_file_out1, self.sam_file_out2,
                                                     self.vcf_to_check1, self.vcf_to_check2, self.seqs_out_dnadiff1,
                                                     self.seqs_out_dnadiff2, self.sam_summary, self.flank_length,
                                                     allow_mismatches=self.allow_flank_mismatches,
                                                     exclude_regions1=self.exclude_regions1,
                                                     exclude_regions2=self.exclude_regions2,
                                                     max_soft_clipped=self.max_soft_clipped,
                                                     number_ns=self.number_ns)
        stats, gt_conf_hist = DnadiffMappingBasedVerifier._gather_stats(self.sam_summary)
        #os.unlink(self.seqs_out_dnadiff1)
        #os.unlink(self.seqs_out_dnadiff2)
        #for f in glob.glob(self.vcf_to_check1 + '*'):
        #    os.unlink(f)
        #for f in glob.glob(self.vcf_to_check2 + '*'):
        #    os.unlink(f)

        # write stats file
        with open(self.stats_out, 'w') as f:
            keys = stats.keys()
            print(*keys, sep='\t', file=f)
            print(*[stats[x] for x in keys], sep='\t', file=f)


        # write GT_CONF histogram files
        with open(self.gt_conf_hist_out, 'w') as f:
            print('GT_CONF\tCount', file=f)
            for gt_conf, count in sorted(gt_conf_hist.items()):
                print(gt_conf, count, sep='\t', file=f)
