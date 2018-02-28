import logging
import os
import shutil

from cluster_vcf_records import vcf_file_read

from minos import utils

class Error (Exception): pass

class MultiSamplePipeline:
    def __init__(self,
        ref_fasta,
        input_data_tsv,
        output_dir,
        min_large_ref_length=50,
        nextflow_config_file=None,
        nextflow_work_dir=None,
        force=False,
    ):
        self.ref_fasta = os.path.abspath(ref_fasta)
        if not os.path.exists(self.ref_fasta):
            raise Error('Reference FASTA file not found: ' + ref_fasta)

        self.input_data_tsv = os.path.abspath(input_data_tsv)
        if not os.path.exists(self.input_data_tsv):
            raise Error('Data TSV file not found: ' + input_data_tsv)

        self.output_dir = os.path.abspath(output_dir)
        self.nextflow_config_file = None if nextflow_config_file is None else os.path.abspath(nextflow_config_file)
        self.min_large_ref_length = min_large_ref_length

        if nextflow_work_dir is None:
            self.nextflow_work_dir = os.path.join(self.output_dir, 'nextflow.work')
        else:
            self.nextflow_work_dir = os.path.abspath(nextflow_work_dir)

        self.force = force
        self.nextflow_input_tsv = os.path.join(self.output_dir, 'nextflow.input.tsv')
        self.nextflow_script = os.path.join(self.output_dir, 'pipeline.script.nf')


    @classmethod
    def _load_input_data_tsv(cls, infile):
        logging.info('Start reading file ' + infile)
        data = []
        with open(infile) as f:
            for line in f:
                try:
                    vcf_file, *reads_files = line.rstrip().split('\t')
                except:
                    raise Error('Bad line in input TSV file: ' + line.rstrip())

                if not(os.path.exists(vcf_file)):
                    raise Error('VCF file not found: ' + vcf_file)
                for reads_file in reads_files:
                    if not(os.path.exists(reads_file)):
                        raise Error('Reads file not found: ' + reads_file)

                data.append((os.path.abspath(vcf_file), [os.path.abspath(x) for x in reads_files]))

        logging.info('Finish reading file ' + infile + '. Loaded ' + str(len(data)) + 'samples')
        return data


    @classmethod
    def _write_nextflow_data_tsv(cls, data, outfile):
        with open(outfile, 'w') as f:
            print('sample_id', 'vcf_file', 'reads_files', sep='\t', file=f)
            for i, (vcf_file, reads_files) in enumerate(data):
                print(i, vcf_file, ' '.join(reads_files), sep='\t', file=f)


    @classmethod
    def _write_nextflow_script(cls, outfile):
        with open(outfile, 'w') as f:
            print(r'''params.data_in_tsv = ""
params.ref_fasta = ""
params.min_large_ref_length = 0


data_in_tsv = file(params.data_in_tsv).toAbsolutePath()
ref_fasta = file(params.ref_fasta).toAbsolutePath()

if (!data_in_tsv.exists()) {
    exit 1, "Input data TSV file not found: ${params.data_in_tsv} -- aborting"
}

if (!ref_fasta.exists()) {
    exit 1, "Reference FASTA file not found: ${params.ref_fasta} -- aborting"
}

if (params.min_large_ref_length < 1) {
    exit 1, "Must use option --min_large_ref_length -- aborting"
}


split_tsv = Channel.from(data_in_tsv).splitCsv(header: true, sep:'\t')


process split_vcf_file {
    input:
    val tsv_fields from split_tsv

    output:
    file("small_vars.${tsv_fields['sample_id']}.vcf") into split_vcf_file_out_small
    set(val(tsv_fields), file("big_vars.${tsv_fields['sample_id']}.vcf")) into merge_small_and_large_vars_in

    """
    #!/usr/bin/env python3
    from minos import vcf_file_split_deletions
    splitter = vcf_file_split_deletions.VcfFileSplitDeletions("${tsv_fields.vcf_file}", "small_vars.${tsv_fields['sample_id']}.vcf", "big_vars.${tsv_fields['sample_id']}.vcf", min_large_ref_length=${params.min_large_ref_length})
    splitter.run()
    """
}


process cluster_small_vars_vcf {
    input:
    val(file_list) from split_vcf_file_out_small.collect()

    output:
    file 'small_vars_clustered.vcf'

    """
    #!/usr/bin/env python3
    from cluster_vcf_records import vcf_clusterer
    file_list = ["${file_list.join('", "')}"]
    clusterer = vcf_clusterer.VcfClusterer(file_list, "${ref_fasta}", "small_vars_clustered.vcf")
    clusterer.run()
    """
}

''', file=f)


    def _prepare_nextflow_input_files(self):
        if os.path.exists(self.output_dir):
            if self.force:
                shutil.rmtree(self.output_dir)
            else:
                raise Error('Error! Output directory already exists. ' + self.output_dir)

        os.mkdir(self.output_dir)
        input_data = MultiSamplePipeline._load_input_data_tsv(self.input_data_tsv)
        MultiSamplePipeline._write_nextflow_data_tsv(input_data, self.nextflow_input_tsv)


    def run(self):
        self._prepare_nextflow_input_files()
        original_dir = os.getcwd()
        os.chdir(self.output_dir)
        nextflow_script = 'run_pipeline.nf'
        MultiSamplePipeline._write_nextflow_script(nextflow_script)
        logging.info('Prepared nextflow files. cd ' + self.output_dir)
        nextflow_command = ' '.join([
            'nextflow run',
            '-work-dir', self.nextflow_work_dir,
            '-with-dag', 'nextflow.out.dag.pdf',
            '-with-trace', 'newxtflow.out.trace.txt',
            nextflow_script,
            '--ref_fasta', self.ref_fasta,
            '--data_in_tsv', self.nextflow_input_tsv,
            '--min_large_ref_length', str(self.min_large_ref_length),
        ])
        logging.info('Start running nextflow: ' + nextflow_command)
        utils.syscall(nextflow_command)
        logging.info('Finish running nextflow. cd ' + original_dir)
        os.chdir(original_dir)
