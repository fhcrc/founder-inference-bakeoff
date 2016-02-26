#!/usr/bin/env python
'''
Consoldate all the toi estimates into asingle pandas dataframe with appropriate indicies.
'''
from __future__ import print_function
import argparse
import os.path
from  itertools import ifilter
import  itertools
from Bio import SeqIO, Alphabet
from Bio import Phylo	# for reading Prank trees
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import IUPAC
import sys
import re
import subprocess
import tempfile
import fnmatch
import pandas as pd
import numpy as np
import logging
import csv
import multiprocessing
import exceptions

# logging.basicConfig(stream=sys.stdout)
log = logging.getLogger(__name__)
a = None # reserved for arguments
columns = ('root', 'c_score', 'c_identity', 'c_gaps', 'c_len', 'b_score', 'b_identity', 'b_gaps', 'b_len', 'pdna_score', 'pdna_identity', 'pdna_gaps', 'pdna_len')
columns = ('method', 'root', 'score', 'identity', 'gaps', 'len')

# set pandas width for printing tables
# http://stackoverflow.com/a/11711637/1135316
pd.set_option('display.width', 200)

def parse_args():
    ''' returns command-line arguments parsed by argparse.
        >>> args = parse_args()
    '''

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-v', '--verbose', dest='verbose', action='store_true', default=False)
    parser.add_argument('-d', '--debug', dest='debug', action='store_true', default=False)
    parser.add_argument('-p', '--processes', default=2)
    return parser.parse_args()

def needle_records(fh):
    """
    separate output form needleman-wunsch alignment into individual records.

    We are only interested in the statistics asociated with each
    alignment, so discard all the details of specific base alignment.

    # Length: 2740
    # Identity:    2740/2740 (100.0%)
    # Similarity:  2740/2740 (100.0%)
    # Gaps:           0/2740 ( 0.0%)
    # Score: 13700.0

    Wish there were a way to make needle be quiet and just output the
    parts we need.  All I need is the score.
    """
    recordsep = '#======================================='
    for line in fh:
        if line.startswith(recordsep):
            record = ''
            for line in fh:
                if line.startswith(recordsep):
                    yield record
                    break
                record += line


from collections import namedtuple
NWScore = namedtuple('NWScore', 'score identity gaps length')

def needle_score(seq1, seq2):
    """Calculate needlman-wunsch score for aligning two sequences.
    """
    # yield NWScore(0,0,0,0)
    # return
    tempfile.tempdir = "./tmp"
    ntf = tempfile.NamedTemporaryFile
    with ntf(prefix='seq1', delete=True) as fh1, \
         ntf(prefix='seq2', delete=True) as fh2, \
         ntf(prefix='align_out', delete=True) as outfile:
        SeqIO.write(seq1, fh1, 'fasta')
        fh1.flush()
        SeqIO.write(seq2, fh2, 'fasta')
        fh2.flush()
        # invoke Needleman-Wunsch global alignment of two sequences from Emboss toolkit
        # http://emboss.sourceforge.net/apps/release/6.3/emboss/apps/needle.html
        # expect to find this in /home/matsengrp/local/bin/needle
        # NB the -gapopen and -gapextend parameters are NOT optional!
        # needle claims default values for these, but will not run if they are not specified.
        # same with -outfile.
        cmd = ['srun', 'needle', '-gapopen', '10.0', '-gapextend', '0.5',  
               '-outfile',  outfile.name,
               fh1.name, fh2.name]
        logging.info(' '.join(cmd))
        subprocess.check_call(cmd, stderr=outfile)

        score_pattern = re.compile(r'# Score: (.*)')
        length_pattern = re.compile(r'# Length: (.*)')
        gaps_pattern = re.compile(r'# Gaps:\s+(\d+)/(\d+)')
        ident_pattern = re.compile(r'# Identity:\s+(\d+)/(\d+)')

        for record in needle_records(outfile):
            # Parse numeric statistics values from N-W text record.
            score = score_pattern.search(record)
            score = float(score.group(1)) if score else 0

            length = length_pattern.search(record)
            length = float(length.group(1)) if length else 0

            gaps = gaps_pattern.search(record)
            gaps = float(gaps.group(1))/float(gaps.group(2)) if gaps else 0
        
            identity = ident_pattern.search(record)
            identity = float(identity.group(1))/float(identity.group(2)) if identity else 0
            yield NWScore(score, identity, gaps, length)


def calculate_needle_score(founder, seq_iter):
    """
    Calculate various scores for comparing founder to sequences from 'seq_iter'

    :param founder: a single SeqIO.record object
    :param seq_iter: generator of SeqIO.Record objects
    :returns: tuple of scores
    :rtype: tuple of floats
    """
    # needle_score() is a generator function that will yield scores for all comparisons between 'founder' and
    # sequences in 'seq_iter'
    scores = list(needle_score(founder, seq_iter))

    # throw the scores into a dataframe and take the average of each column.
    df = pd.DataFrame.from_records(scores)
    return df.mean()

def parse_log(fp, burnin=0.9):
    """
    generator function for parsing ancestral founder sequences from BEAST trait file.

    The file expected to be a set of ancestral founder sequences from Beast.
    Each non-comment line consists of an iteration count followed by some number or quoted founder sequences.
    """

    # first count how many sequences are in the file so we can figure out how many to skip for burn-in.
    count = 0
    for line in csv.reader(fp, delimiter='\t'):
        if line[0].startswith('#'):
            continue
        if not line[0].isdigit():
            continue
        count += 1

    skip = round(count * burnin)
    logging.debug("beast skipping {}/{} sequences for burning, keeping {}".format(skip,count, count-skip))
    
    # Reposition pointer at the beginning once again
    fp.seek(0, 0);
    for line in csv.reader(fp, delimiter='\t'):
        if line[0].startswith('#'):
            continue
        if not line[0].isdigit():
            continue
        if skip > 0:
            skip -= 1
            continue
        yield line


def beast_iter(root, burnin):
    """
    iterate over BEAST inferred founder sequences

    This routine knows how to retrieve and iterate over sequences
    produced by Beast.  It yields a series of sequences corresponding to
    high posterior probability after a 10% burn-in period.

    :param root: string name of directory where sequence files can be found.
    :returns: iterator over seqeuences
    :rtype:
    """

    def str2Seq(s, id='dynamic'):
        """Our distance measure takes a Bio.Seq object,
        but we will be reading strings from the beast ancestror log.
        Define a routine to do the conversion for us.
        """
        return SeqRecord(Seq(s, IUPAC.IUPACUnambiguousDNA),
                  id=id, name="anonymous",
                  description="dynamically created sequence from ancestralSequences.log")
    
    beast = os.path.join(root, 'ancestralSequences.log')
    logging.debug("iterating beast {}".format(beast))

    with open(beast, "rb") as fh:
        # NB parse_log() will skip a burn-in period
        for seq in parse_log(fh, burnin=burnin):
            yield str2Seq(seq[1], id=seq[0])


def prank_dna_iter(root):
    """
    iterate over Prank founder sequence(s) inferred from DNA model

    This routine knows how to retrieve and iterate over sequences
    produced by Prank. It will yield a single sequence corresponding to
    the root of the guide tree.

    :param root: string name of directory where sequence files can be found.
    :returns: iterator over seqeuences
    :rtype:
    """

    # identify the sequence associated with the root node.
    prankdir = os.path.join(root, 'prank_dna')
    tree = Phylo.read(os.path.join(prankdir, 'prank.best.anc.dnd'), 'newick')
    rootid = str(tree.root)
    record_dict = SeqIO.index(os.path.join(prankdir, 'prank.best.anc.fas'), "fasta")
    yield record_dict[rootid] 


def prank_codon_iter(root):
    """
    iterate over Prank founder sequence(s) inferred from CODON model
    See https://github.com/cswarth/hiv-sim/issues/2

    This routine knows how to retrieve and iterate over sequences
    produced by Prank. It will yield a single sequence corresponding to
    the root of the guide tree.

    :param root: string name of directory where sequence files can be found.
    :returns: iterator over seqeuences
    :rtype:
    """

    # identify the sequence associated with the root node.
    prankdir = os.path.join(root, 'prank_codon')
    tree = Phylo.read(os.path.join(prankdir, 'prank.best.anc.dnd'), 'newick')
    rootid = str(tree.root)
    record_dict = SeqIO.index(os.path.join(prankdir, 'prank.best.anc.fas'), "fasta")
    yield record_dict[rootid] 


def consensus_iter(root):
    """
    calculate a single consensus sequence of both donor and recipient sequences 
    """
    from Bio.Align.Generic import Alignment
    from Bio.Alphabet import IUPAC, Gapped
    from Bio.Align import AlignInfo
    from Bio import AlignIO

    consensus = ""
    maffile = os.path.join(root, 'sequences.maf')
    with open(maffile) as fh:
        alignment = AlignIO.read(maffile, "fasta")
        summary_align = AlignInfo.SummaryInfo(alignment)
        # consensus = summary_align.gap_consensus()
        m = summary_align.pos_specific_score_matrix()

        def cons(row):
            # each row holds abundances of each nucleotide or gap.
            mm = max([v for v in row.values()])
            vv = [k for k,v in row.items() if mm == v]
            if len(vv) > 1:
                if '-' in vv:
                    c = '?'
                else:
                    c = 'X'
            else:
                c = vv[0]
            return c

        consensus = "".join([cons(row) for row in m])

        # <class 'Bio.Align.AlignInfo.PSSM'>
        # You can access a single element of the PSSM using the following:

        # your_pssm[sequence_number][residue_count_name]
        # For instance, to get the 'T' residue for the second element in the above alignment you would need to do:

        # your_pssm[1]['T']
        # A  0.0 20.0 0.0 0.0 0.0
        # X  12.0 4.0 0.0 2.0 2.0
        # G  12.0 0.0 0.0 6.0 2.0
        # A  8.0 9.0 2.0 1.0 0.0
        # T  0.0 0.0 2.0 0.0 18.0
        # T  0.0 4.0 1.0 0.0 15.0
        # A  0.0 20.0 0.0 0.0 0.0
        # X  0.0 10.0 0.0 10.0 0.0


    # 'consensus' is just a string made from the consensus base calculated
    # across all sites from donor and recipient sequences.  although this used
    # yield instead of return, it is always returning a single sequence.
    
    yield SeqRecord(Seq(consensus), id='consensus', description=root)

def calculate_control_score(founder, root):
    # cscore is a control value that increases monotonically with donor and recipient distance.
    # it is used to validate plotting methods.
    # for now have it indicate the total distance between samples, so just the sum of donor and recipient dimes since infection.
    def is_number(s):
        """return true if argument looks like a python number, false otherwise."""
        try:
            float(s)
            return True
        except ValueError:
            return False
    tokens = root.split("/")
    donor = int(tokens[-2]) if is_number(tokens[-2]) else 0
    recipient = int(tokens[-1]) if is_number(tokens[-1]) else 0
    cscore = donor+recipient
    return cscore


# NB this routine MUST be a top-level routine!
#
# Because this routine is called via routines in the 'multiprocessing' package, it must
# not be buried inside another routine or it won't be visible.  The failure
# mode is complete silence from the multiprocessing routines.
def _process_dir(founder, dir):
    """
    Calculate distance between founder & inferred sequences under directories 'dirs'

    Calculates distance measures between 'founder' and sequences
    inferred by several algorithms, including Prank and Beast.  For
    Prank files, only a single inferred founder is extracted.  For BEAST
    files we calculate scores for a number of high-posterior-probabilty
    founders and average the results.

    Higher scores are better.
"""
    try:
        prank_dna = calculate_needle_score(founder, prank_dna_iter(dir))
        prank_codon = calculate_needle_score(founder, prank_codon_iter(dir))
        cons_data = calculate_needle_score(founder, consensus_iter(dir))
        beast_data = calculate_needle_score(founder, beast_iter(dir, burnin=0.40))
        m = np.vstack((cons_data, beast_data, prank_dna, prank_codon))
        df = pd.DataFrame(m, columns=('nw_score', 'pct_identity', 'pct_gaps', 'len'))
        df['method'] = pd.Series(['consensus', 'beast', 'prank_dna', 'prank_codon'], index=df.index)
        df['dir'] = pd.Series(dir, index=df.index)
    except exceptions.IOError as e:
        logging.info(e)
        df =  None

    return df


# true if need to print header
first = True


def callback(df):
    global first
    if df is not None:
        df.to_csv(sys.stdout, index=False, header=first)
        first = False


def process_founder(founder, dirs, nproc):
    """Calculate distance between founder & inferred sequences under directories 'dirs'

    This routine calculates distance measures between 'founder' and 
    sequences inferred by several algorithms, including Prank and
    Beast.

    For each inferred founder file,
    identify the root node and calculate the score between the root
    and the founder.  Higher scores are better.
    """
    global a
    global columns
    if a.debug:
        map(callback, [_process_dir(founder, dir) for dir in dirs])
    else:
        pool = multiprocessing.Pool(nproc)
        results = [pool.apply_async(_process_dir, args = (founder, dir), callback=callback) for dir in dirs]
        pool.close()
        pool.join()
        #result_list = [r.get() for r in results]

    # result_list = [r for r in result_list if r is not None]
    # df = None
    # if result_list:
    #     df = pd.concat(result_list)
    # return(df)    

# recursively generate full path names of files matching 'name' below directory 'dir'.
def find_file(name, dir=os.path.curdir):
    for root, dirnames, filenames in os.walk(dir):
        if name in filenames:
            yield os.path.join(root, name)
        for d in dirnames:
            for path in find_file(name, dir=os.path.join(root, d)):
                yield path


# read and concatenate Time Of Infection (TOI) estimates 
def toi_rows(files):
    for p in files:
        df = pd.read_csv(p, header=None, names=['mean','+var','-var'])
        df['path'] = p
        tokens = p.split(os.path.sep)
        df['replicate'] = int(tokens[1])
        df['rate'] = float(tokens[2].split('_')[1])
        df['backoff'] = float(tokens[3].split('_')[1])
        df['data'] = (tokens[4] == 'data')
        df['experimental'] = (tokens[5] == 'experimental')
        yield df
        
def main():
    loglevel = "ERROR"
    global a
    a = parse_args()

    if a.verbose:
        loglevel = "INFO"
    if a.debug:
        loglevel = "DEBUG"

    numeric_level = getattr(logging, loglevel.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % loglevel)
    logging.basicConfig(level=numeric_level, stream=sys.stderr)

    logging.info("Info")
    logging.debug("Debug")

    from datetime import datetime

    print("# Created by {} on {}".format(os.path.basename(__file__), datetime.now().strftime("%Y-%m-%d %H:%M")))
    
    print("# dir = sample directory")
    print("# method = method of founder inference (consensus, beast, prank_dna, and prank_codon)")
    print("# nw_score = Needleman-Wunsch global alignment between the infered founder sequence and the actual founder.")
    print("# 			The Needleman-Wunsch algorithm is implemented in the Emboss toolkit.")
    print("# 			The \"EDNAFULL\" scoring matrix is used to score DNA comparisons with a gap open penalty of 10.0 and gap extension penalty of 0.5.")
    print("# pct_gaps = proportion gap positions between inferred and actual founder")
    print("# pct_identity = proportion identical sites between inferred and actual founder")
    print("# len = avg. length of inferred founder")
            
    first = True
    tbl = None
    df = pd.concat(toi_rows(find_file('toi.csv')))
    print(df)
    
        
            
    
if __name__ == '__main__':

    main()


