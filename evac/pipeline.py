# -*- coding: utf-8 -*-
"""Aligner-agnostic alignment pipeline that reads from SRA.
"""
from contextlib import contextmanager
import copy
import os
from subprocess import Popen, PIPE

from evac.utils import *

from ngs import NGS
from ngs.Read import Read
from ngs.ErrorMsg import ErrorMsg

# Readers

def sra_read_pair(read_pair):
    """Creates a pair of tuples (name, sequence, qualities) from the current
    read of an ngs.ReadIterator.
    """
    read_name = read_pair.getReadName()
    if read_pair.getNumFragments() != 2:
        raise Exception("Read {} is not paired".format(read_name))
        
    read_group = read_pair.getReadGroup()
    
    read_pair.nextFragment()
    if not read_pair.isPaired():
        raise Exception("Read {} is not paired".format(read_name))
    read1 = (
        read_name,
        read_pair.getFragmentBases(),
        read_pair.getFragmentQualities())
    
    read_pair.nextFragment()
    if not read_pair.isPaired():
        raise Exception("Read {} is not paired".format(read_name))
    read2 = (
        read_name,
        read_pair.getFragmentBases(),
        read_pair.getFragmentQualities())
    
    return (read1, read2)

def sra_reader(accn, batch_size=1000, max_reads=None):
    """Iterates through a read collection for a given accession number using
    the ngs-lib python bindings.
    
    Args:
        accn: The accession number
        batch_size: The maximum number of reads to request in each call to SRA
        max_reads: The total number of reads to process, or all reads in the
            SRA run if None
    
    Yields:
        Each pair of reads (see ``sra_read_pair``)
    """
    with NGS.openReadCollection(accn) as run:
        run_name = run.getName()
        read_count = run.getReadCount()
        if max_reads:
            max_reads = min(read_count, max_reads)
        else:
            max_reads = read_count
        for batch_num, first_read in enumerate(
                range(1, max_reads, batch_size)):
            cur_batch_size = min(
                batch_size,
                max_reads - first_read + 1)
            with run.getReadRange(
                    first_read, cur_batch_size, Read.all) as read:
                for read_idx in range(cur_batch_size):
                    read.nextRead()
                    yield sra_read_pair(read)

# Writers

class BatchWriter(object):
    """Wrapper for a string writer (e.g. FifoWriter) that improves performance
    by buffering a set number of reads and sending them as a single call to the
    string writer.
    
    Args:
        writer: The string writer to wrap. Must be callable with two arguments
            (read1 string, read2 string)
        batch_size: The size of the read buffer
        lines_per_row: The number of lines used by each read for the specific
            file format (should be passed by the subclass in a
            super().__init__ call)
        linesep: The separator to use between each line (defaults to os.linesep)
    """
    def __init__(self, writer, batch_size, lines_per_row, linesep=os.linesep):
        self.writer = writer
        self.batch_size = batch_size
        self.lines_per_row = lines_per_row
        self.linesep = linesep
        self.read1_batch = self._create_batch_list()
        self.read2_batch = copy.copy(self.read1_batch)
        self.index = 0
    
    def _create_batch_list(self):
        """Create the list to use for buffering reads. Can be overridden, but
        must return a list that is of size ``batch_size * lines_per_row``.
        """
        return [None] * (self.batch_size * self.lines_per_row)
    
    def __call__(self, read1, read2):
        """Add a read pair to the buffer. Writes the batch to the underlying
        writer if the buffer is full.
        
        Args:
            read1: read1 tuple (name, sequence, qualities)
            read2: read2 tuple
        """
        self.add_to_batch(*read1, self.read1_batch, self.index)
        self.add_to_batch(*read2, self.read2_batch, self.index)
        self.index += lines_per_row
        if self.index >= self.batch_size:
            self.flush()
    
    def __enter__(self):
        return self
    
    def __exit__(exception_type, exception_value, traceback):
        if self.index > 0:
            self.flush(last=True)
        self.close()
    
    def flush(self, last=False):
        """Flush the current read buffers to the underlying string writer.
        
        Args:
            last: Is this the last call to flush? If not, a trailing linesep
                is written.
        """
        if self.index < self.batch_size:
            self.writer(
                self.linesep.join(self.read1_batch[0:self.index]),
                self.linesep.join(self.read2_batch[0:self.index]))
        else:
            self.writer(
                self.linesep.join(self.read1_batch),
                self.linesep.join(self.read2_batch))
        if not last:
            self.writer(self.linesep, self.linesep)
        self.index = 0
    
    def close(self):
        """Clear the buffers and close the underlying string writer.
        """
        self.read1_batch = None
        self.read2_batch = None
        self.writer.close()

class FastqWriter(BatchWriter):
    """BatchWriter implementation for FASTQ format.
    """
    def __init__(self, batch_size):
        super(FastqWriter, self).__init__(batch_size, 4)
    
    def _create_batch_list(self):
        return [None, None, '+', None] * self.batch_size
    
    def add_to_batch(name, sequence, qualities, batch, index):
        batch[index] = '@' + name
        batch[index+1] = sequence
        batch[index+3] = qualities

class FifoWriter(object):
    """String writer that opens and writes to a pair of FIFOs.
    
    Args:
        fifo1: Path to the read1 FIFOs
        fifo2: Path to the read2 FIFOs
        kwargs: Additional arguments to pass to the ``open`` call.
    """
    def __init__(self, fifo1, fifo2, **kwargs):
        self.fifo1 = open(fifo1, 'wt', **kwargs)
        self.fifo2 = open(fifo2, 'wt', **kwargs)
    
    def __call__(self, read1_str, read2_str):
        self.fifo1.write(read1_str)
        self.fifo2.write(read2_str)
    
    def close(self):
        for fifo in (self.fifo1, self.fifo2):
            try:
                fifo.flush()
                fifo.close()
            finally:
                os.remove(fifo)

# Pipelines

# TODO: [JD] These have lots of redundant code right now. I will be adding
# alternate readers for local FASTQ and SAM/BAM/CRAM files and refactoring the
# Pipelines to accept an arbitrary reader.

def star_pipeline(args):
    with TempDir() as workdir:
        fifo1, fifo2 = workdir.mkfifos('Read1', 'Read2')
        with open_(args.output_bam, 'wb') as bam:
            cmd = normalize_whitespace("""
                STAR --runThreadN {threads} --genomeDir {index}
                    --readFilesIn {fifo1} {fifo2}
                    --outSAMtype BAM SortedByCoordinate
                    --outStd BAM SortedByCoordinate
                    --outMultimapperOrder Random
                    {extra}
            """.format(
                threads=args.threads,
                index=args.index,
                fifo1=fifo1,
                fifo2=fifo2,
                extra=args.aligner_args
            ))
            with Popen(cmd, stdout=bam) as proc:
                with FastqWriter(FifoWriter(fifo1, fifo2), args.batch_size) as writer:
                    for read_pair in sra_reader(
                            args.sra_accession,
                            batch_size=args.batch_size,
                            max_reads=args.max_reads):
                        writer(*read_pair)
                proc.wait()


# TODO: [JD] The use of pipes and shell=True is insecure and not the recommended
# way of doing things, but I want to benchmark the alternative (chained Popens)
# to make sure it's not any slower.

def hisat_pipeline(args):
    with open_(args.output_bam, 'wb') as bam:
        cmd = normalize_whitespace("""
            hisat2 -p {threads} -x {index} --sra-acc {accn} {extra}
                | sambamba view -S -t {threads} -f bam /dev/stdin
                | sambamba sort -t {threads} /dev/stdin
        """.format(
            accn=args.sra_accession,
            threads=args.threads,
            index=args.index,
            extra=args.aligner_args
        ))
        with Popen(cmd, stdout=bam, shell=True) as proc:
            proc.wait()

def kallisto_pipeline(args):
    with TempDir() as workdir:
        fifo1, fifo2 = workdir.mkfifos('Read1', 'Read2')
        libtype = ''
        if 'F' in args.libtype:
            libtype = '--fr-stranded'
        elif 'R' in args.libtype:
            libtype = '--rf-stranded'
        cmd = normalize_whitespace("""
            kallisto quant -t {threads} -i {index} -o {output}
                {libtype} {extra} {fifo1} {fifo2}
        """.format(
            threads=args.threads,
            index=args.index,
            output=args.output,
            libtype=libtype,
            extra=args.aligner_args,
            fifo1=fifo1,
            fifo2=fifo2))
        with Popen(cmd) as proc:
            with FastqWriter(FifoWriter(fifo1, fifo2), args.batch_size) as writer:
                for read_pair in sra_reader(
                        args.sra_accession,
                        batch_size=args.batch_size,
                        max_reads=args.max_reads):
                    writer(*read_pair)
            proc.wait()

def salmon_pipeline(args):
    with TempDir() as workdir:
        fifo1, fifo2 = workdir.mkfifos('Read1', 'Read2')
        cmd = normalize_whitespace("""
            salmon quant -p {threads} -i {index} -l {libtype}
                {extra} -1 {fifo1} -2 {fifo2} -o {output}
        """.format(
            threads=args.threads,
            index=args.index,
            libtype=args.libtype,
            output=args.output,
            extra=args.aligner_args,
            fifo1=fifo1,
            fifo2=fifo2))
        with Popen(cmd) as proc:
            with FastqWriter(FifoWriter(fifo1, fifo2), args.batch_size) as writer:
                for read_pair in sra_reader(
                        args.sra_accession,
                        batch_size=args.batch_size,
                        max_reads=args.max_reads):
                    writer(*read_pair)
            proc.wait()

def mock_pipeline(args):
    for read1, read2 in sra_reader(
            args.sra_accession,
            batch_size=args.batch_size,
            max_reads=args.max_reads):
        print(read1[0] + ':')
        print('  ' + '\t'.join(read1[1:]))
        print('  ' + '\t'.join(read2[1:]))

pipelines = dict(
    star=star_pipeline,
    hisat=hisat_pipeline,
    kallisto=kallisto_pipeline,
    salmon=salmon_pipeline,
    mock=mock_pipeline)

# Main interface

def list_pipelines():
    """Returns the currently supported pipelines.
    
    Returns:
        A list of pipeline names
    """
    return list(pipelines.keys())

def run_pipeline(args):
    """Run a pipeline using a set of command-line args.
    
    Args:
        args: a Namespace object
    """
    pipeline = pipelines[args.pipeline]
    pipeline(args)
