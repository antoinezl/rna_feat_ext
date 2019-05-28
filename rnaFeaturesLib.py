# -*- coding: utf-8 *-*
"""rnaFeaturesLib: A pure Python module for transcripts RNA fetures extraction from ENSEMBL derived from a list of ENSEMBL gene IDs.

Authors: Costas Bouyioukos, Franz-Arnold Ake and Antoine Lu, 2018-19, Paris UMR7216."
"""
__version__ = "0.3a08"

import os
import sys
import io
import re
import math
import tempfile
import shlex
import shutil
import subprocess
from biomart import BiomartServer
import pandas as pd
from Bio import SeqIO
from Bio.SeqUtils import GC
from Bio.SeqUtils import CodonUsage
from Bio.SeqUtils.CodonUsage import CodonAdaptationIndex
from collections import namedtuple

import local_score



# CLASSES Interface.
class ENSEMBLSeqs(object):
    """Class to represent RNA related sequence features from ENSEMBL.

    Needs a Bio.Seq.Record generator object to initialise."""

    def __init__(self, bioSeqRecsGen, exprTrans):
        self.gen = bioSeqRecsGen
        if exprTrans:
            self.exprTranscripts = self.parse_expressed_transcripts(exprTrans)
        else:
            self.exprTranscripts = None
        self.bioSeqRecs = self.get_bio_seqrec()

    def get_bio_seqrec(self):
        """Expands the Bio.Seq.Rec generator to a list of Bio.Record objects.

        Also puts some SeqIO.record member variables in place, namely the id, the gene name the description and the features."""

        def _construct_bio_seq(recs, rec):
            """Auxiiary function to construct a Bio.Seq object."""
            # Extract the gene name.
            rec.name = rec.description.split("|")[2].split(":")[1]
            descr = rec.description.split("|")[-1]
            # Extract all the features as a dictionary (apart from the last one which was the description.).
            feat = dict(item.split(":") for item in rec.description.split("|")[1:-1])
            rec.features = feat  # TODO for the moment all the features are stored as a dictionary and not as proper SeqFeature objects. (perhaps we can stick with that and there is no need to change it.)
            rec.description = descr
            recs.append(rec)

        recs = []
        for rec in self.gen:
            if self.exprTranscripts is None:
                _construct_bio_seq(recs, rec)
            if self.exprTranscripts and (rec.id in self.exprTranscripts):
                _construct_bio_seq(recs, rec)
        return recs

    def parse_expressed_transcripts(self, exprTrans):
        """Parses the given transcript expression file and returns a set of the expressed transcripts for quicker lookup."""
        trDf = pd.read_table(exprTrans, header=None)  # We do not really care about headers we only need the first column.
        return set(trDf[0])


class FeaturesExtract(object):
    """Claas to extract features."""

    def __init__(self, bioSeqRecs, options):
        """Initialise with a list of SeqIO records."""
        self.bioSeqRecs = bioSeqRecs
        # Temporary files of the UTRs
        self.tf5p = tempfile.NamedTemporaryFile(mode="a", delete=False)
        self.tf3p = tempfile.NamedTemporaryFile(mode="a", delete=False)
        self.tfCoding = tempfile.NamedTemporaryFile(mode="a", delete=False)
        self.utr3len = options.utr3len
        self.utrFiles = options.utrFiles
        self.clip = options.clip

    def collect_features(self):
        """Collect the features that do not need external computations.

        Return a pandas data frame."""
        # Initialise a pabdas data frame.
        pdf = pd.DataFrame(columns=['ensembl_gene_id', 'gene_name', 'coding_len', '5pUTR_len', '5pUTR_GC', '3pUTR_len', '3pUTR_GC', 'Kozak_Sequence', 'Kozak_Context'])
        for rec in self.bioSeqRecs:
            # Fetch UTRs, lenghts and GCs
            res3 = get_3utr(rec, self.tf3p, self.utr3len)
            if res3:  # Conditon for 3pUTR length.
                utr3len, utr3gc = res3
                utr5len, utr5gc = get_5utr(rec, self.tf5p)
                # Get Kozaks
                seqKozak, contKozak = get_kozak(rec)
                # Length of coding region.
                codeLen = int(rec.features["cDNA_end"]) - int(rec.features["cDNA_start"])
                get_coding(rec, self.tfCoding)
                # Add to pandas data frame.
                pdfe = [rec.features["GeneID"], rec.name, codeLen, utr5len, "{0:.2f}".format(utr5gc), utr3len, "{0:.2f}".format(utr3gc), seqKozak, contKozak]
                pdf.loc[rec.id] = pdfe
            else:
                continue
        self.tf5p.close()
        self.tf3p.close()
        self.tfCoding.close()
        return pdf

    def calculate_features(self):
        """Method to perfom feature calculation.
        Invokes external software to make calculations that separates it from the previous method.

        Return a pandas data frame with the calculations."""
        # Calculates 5pUTR folding etc.
        fe5p = calculate_free_energy(self.tf5p.name, "5pUTR")
        fe3p = calculate_free_energy(self.tf3p.name, "3pUTR")
        # Calculate local score
        scoring = {'A':-1, 'C':1, 'G':-1, 'T':1, "N":0}  # Specify a local score in such a way that will endorse TOP mRNAs.
        # Compute teh local score for TOP mRNAs.
        ls5p = calculate_local_score(self.tf5p.name, scoring, self.clip)
        # Calculate CAI
        caiCod = calculate_CAI(self.tfCoding.name)
        #TODO Calculate bind motifs
        # motifs = predictBinding()
        # Merge data frames and return.
        return pd.concat([fe5p, fe3p, ls5p, caiCod], axis=1, sort=False)

    def __del__(self):
        """Cleanup the temp files"""
        if self.utrFiles:
            shutil.move(self.tf5p.name, self.utrFiles[0])
            shutil.move(self.tf3p.name, self.utrFiles[1])
        else:
            os.remove(self.tf5p.name)
            os.remove(self.tf3p.name)



## FUNCTIONS
def get_ENSEMBL_data(listID, dataset, transctip_expr_file = None):
    """Function to connect to ENSEBL and retrieve data.

    The functions follows two modes of working:
    1) Transcript selection scheme acording to ENSEMBL/HAVANA, TSL and APPRIS. (Default)
    2) Transcript selection is based on the best expressed transcript from the externaly provided file.

    Return a data frame of the transcripts and their ENSEMBL features."""
    print("Connection to ENSEMBL server.", file=sys.stderr)
    server = BiomartServer("http://www.ensembl.org/biomart/")
    dt = server.datasets[dataset]
    print(f"Retrieve the dataset...", file=sys.stderr)
    listAttrib = ['ensembl_gene_id', 'ensembl_transcript_id',
                  'external_gene_name', 'transcript_length', 'transcript_biotype', 'cdna_coding_start', 'cdna_coding_end', 'cdna', 'description']
    listAttrib2 = ['ensembl_gene_id', 'ensembl_transcript_id', 'transcript_tsl',
                   'transcript_appris', 'transcript_source', 'transcript_length',
                   'transcript_biotype']
    print("Fetch data from: {}".format(str(server)), file=sys.stderr)
    # Collect data from the ENSEMBL datasets.
    dfFeat = pd.DataFrame()
    dfTrans = pd.DataFrame()
    for chunk in chunks(listID, 100):
        print('Fetching...', file=sys.stderr)
        res1 = dt.search({'filters': {'ensembl_gene_id': chunk}, 'attributes': listAttrib}, header=1)
        res2 = dt.search({'filters': {'ensembl_gene_id': chunk}, 'attributes': listAttrib2}, header=1)
        # Reading stream to a pandas data frame.
        dataf = pd.read_table(io.StringIO(res1.text), sep='\t', encoding='utf-8')
        datat = pd.read_table(io.StringIO(res2.text), sep='\t', encoding='utf-8')
        # Cleanup data frame lines that do not correspond to protein coding genes.
        dataf = dataf[dataf['Transcript type'] == 'protein_coding']
        datat = datat[datat['Transcript type'] == 'protein_coding']
        # Concatenate data frames.
        dfFeat = pd.concat([dfFeat, dataf], axis=0, sort=False)
        dfTrans = pd.concat([dfTrans, datat], axis=0, sort=False)
    print("...fetch done!", file=sys.stderr)
    #TODO Here we can integrate later the selection step based on gene expression!
    # The trascript selection step.
    trans_sorted = transcript_classification(dfTrans)
    transcripts = pd.DataFrame()
    # Set the index to the transcript ID
    dfFeat.set_index('Transcript stable ID', inplace=True)
    dfTrans.set_index('Transcript stable ID', inplace=True)
    # Concatenate the two data frames by setting the index to the outer product.
    dfENSEMBL = pd.concat([dfFeat, dfTrans], axis=1, join='inner')
    #dfENSEMBL.reset_index(inplace=True)
    dfENSEMBL = dfENSEMBL.T.drop_duplicates().T
    # The actual selction loop
    for gene in trans_sorted.keys():
        for trans in trans_sorted[gene]:
            row = dfENSEMBL.loc[trans.trans_id]
            if row.isnull().any(): continue
            else:
                row = pd.DataFrame(row).T
                row.reset_index(inplace=True)
                transcripts = transcripts.append(row, ignore_index=True)  # Strange way to concatenate a data frame.
                break
    transcripts.set_index('index', inplace=True)
    # Remove the transcript type column.
    transcripts.drop('Transcript type', axis=1, inplace=True)
    # Set the size of the UTRs and the CDS by using the information of the coding exon.
    for index, row in transcripts.iterrows():
        # For 5'UTR end take the smallest coordinate in the coding exons.
        coding_start = min([int(x) for x in row["cDNA coding start"].split(";")])
        coding_end = max([int(x) for x in row["cDNA coding end"].split(";")])
        # REPLACE the right CDS start and end
        transcripts.at[index, "cDNA coding start"] = coding_start
        transcripts.at[index, "cDNA coding end"] = coding_end
        # Clean up the TSL value.
        tsl = row["Transcript support level (TSL)"].split()[0]
        transcripts.at[index, "Transcript support level (TSL)"] = tsl
    return transcripts


def transcript_classification(ensemblTable):
    """Return a dictionary of transcripts per gene, sorted by the ENSEMBL classification based on the Havana-APPRIS-TSL (with this order) criteria.

    ensemblTable: A pandas dataframe with the ENSEMBL transcript classification. It must contain 5 columns.
    return: A dicitonary of key=geneID : value:[sorted list of transcripts]
    """
    # Declare the namedtuple.
    Transcript = namedtuple('Transcript', "trans_id, havana, appris, tsl, length, sort")
    genes_transcripts = {}
    havanaDict = {"ensembl_havana" : 0, "ensembl" : 1, "havana" : 2}
    for i in range(len(ensemblTable)):
        # Collect the attributes
        cid = ensemblTable.iloc[i, 0]
        tr_id = ensemblTable.iloc[i, 1]
        tsl = ensemblTable.iloc[i, 2]
        appris = ensemblTable.iloc[i, 3]
        havana = ensemblTable.iloc[i, 4]
        tr_len = ensemblTable.iloc[i, 5]
        # Generate the sorting tuple.
        # Deal with the transcript source.
        if havana in havanaDict:
            h = havanaDict[havana]
        else:
            h = math.inf
        # Deal with the APPRIS characterisation.
        mm = re.match(r"^([a-z]+)([0-9]+)", str(appris))
        if mm:
            a = ""
            if mm.group(1) == "principal":
                a = a + "a"
            elif mm.group(1) == "alternative":
                a = a + "b"
            a = a + mm.group(2)
        else:
            a = "z"
        # Take the TSL order (Inf if NA)
        tt = re.match(r"^[a-z]{3}([0-9]+)", str(tsl))
        if tt:
            t = int(tt.group(1))
        else:
            t = math.inf
        # reverse Length
        l = -int(tr_len)
        # Form the sorting tuple.
        sort_tuple = (h, a, t, l)
        # Form the namedtuple
        tr_namedtuple = Transcript(tr_id, havana, appris, tsl, tr_len, sort_tuple)
        genes_transcripts.setdefault(cid, [])
        genes_transcripts[cid].append(tr_namedtuple)
    # Sort each list of nametuples in the dict.
    for gene, transcript_list in genes_transcripts.items():
        trListSorted = sorted(transcript_list, key=lambda t: t.sort)
        genes_transcripts[gene] = trListSorted
    return genes_transcripts


def get_kozak(rec, s=10, c=20):
    """Extract both Kozak sequence and context from a SeqIO record.
    (s and c define the extremeties of a Koxak sequence and are chosen by convention.)
    return a tuple of Kozak sequence and Kozak context.

    If ATG is located near the 5'UTR start, it is most likely that either seqs will not be retrieved as self.base[-5:10] returns blank.

    In this case, we test whether the value left to ':' is negative or not.
    If it is < 0, we simply take the seq from 0 as : self.bases[0:self.cDNA_start) + 2) + s]"""
    feat = rec.features
    seq = rec.seq
    # Kozak sequence
    if int(feat["cDNA_start"]) - 1 - s < 0:
        kozakSeq = seq[0:(int(feat["cDNA_start"]) + 2) + s]
    else:
        kozakSeq = seq[(int(feat["cDNA_start"]) - 1 - s):(int(feat["cDNA_start"]) + 2) + s]
    # Kozak context
    if int(feat["cDNA_start"]) - 1 - s - c < 0:
        kozakContext = seq[0:(int(feat["cDNA_start"]) + 2) + s + c]
    else:
        kozakContext = seq[((int(feat["cDNA_start"]) - 1 - s) - c):(int(feat["cDNA_start"]) + 2) + s + c]
    return(str(kozakSeq), str(kozakContext))


def get_3utr(rec, tf3p, lim):
    """Fetch 3PUTR sequence, append it to fasta file.
    Impose limits to UTRs to upto <lim> nucleotides.

    Return its length and GC content."""
    utr3p = rec.seq[int(rec.features["cDNA_end"]):]
    utr3plen = len(utr3p)
    if utr3plen >= lim:
        return None
    if utr3plen:
        tf3p.write(">{}_3PUTR\n{}\n".format(rec.id, utr3p))
    else:
        tf3p.write(">{}_3PUTR\n{}\n".format(rec.id, "N"))
    return(utr3plen, GC(utr3p))


def get_5utr(rec, tf5p):
    """Fetch 5PUTR sequence, append it to fasta file.

    Return its length and GC content."""
    utr5p = rec.seq[0:int(rec.features["cDNA_start"])-1]
    if utr5p:
        tf5p.write(">{}_5PUTR\n{}\n".format(rec.id, utr5p))
    else:
        tf5p.write(">{}_5PUTR\n{}\n".format(rec.id, "N"))
    return(len(utr5p), GC(utr5p))


def get_coding(rec, codf):
    """Fetch coding sequence, append it to fasta file.

    Return: None"""
    cds = rec.seq[int(rec.features["cDNA_start"])-1:int(rec.features["cDNA_end"])]
    if len(cds)%3 != 0:
        #raise StandardError('Coding sequence not a multiple of 3!')
        write("Coding sequence not a multiple of 3!", sys.stderr)
    codf.write(">{}_CDS\n{}\n".format(rec.id, cds))


def calculate_free_energy(ffile, col):
    """Method to perform the free energy calculation by RNAfold and parsing of the results.

    Needs the RNA Vienna package to be installed."""
    pdf = pd.DataFrame(columns=['{}_MFE'.format(col), '{}_MfeBP'.format(col)])
    cmd = 'RNAfold --verbose --noPS --jobs -i {}'.format(ffile)
    proc = subprocess.check_output(shlex.split(cmd))
    proc = proc.decode()  # Convert it to proper string.
    outList = proc.splitlines()  # Retrieve STDOUT.
    for i in range(0, len(outList), 3):
        idt = outList[i][1:-6]  # To exclude the _UTR suffix.
        seq = outList[i+1]
        fold = outList[i+2]
        mfeRE = re.compile(r"[-+]?\d*\.\d+|\d+")
        mfe = float(mfeRE.search(fold).group())
        mfeBp = mfe / float(len(seq))
        pdf.loc[idt] = [mfe, mfeBp]
    return pdf


def calculate_local_score(file, scoring, cliping):
    """Calculate the local score for a given scoring functionself.

    Here we use a scoring for TOP mRNAs and we clip at 50nts."""
    lsdf = pd.DataFrame(columns=["TOP_localScore"])
    for seq in SeqIO.parse(file, "fasta"):
        lss = local_score.local_score(seq.seq, scoring, cliping)
        ls = max(lss)
        idt = seq.id[0:-6]  # To exclude the _UTR suffix.
        lsdf.loc[idt] = [ls]
    return lsdf


def calculate_CAI(file):
    """Calculate the Codon Adaptation Index."""
    caidf = pd.DataFrame(columns=["CAI"])
    SeqCai = CodonUsage.CodonAdaptationIndex()
    # This is a hardcoded dictionary of Human Codon Usage.
    # TODO add the dictionary as an extrenal file.
    cd = {"TTT": 0.46, "TCT": 0.19, "TAT": 0.44, "TGT": 0.46,
          "TTC": 0.54, "TCC": 0.22, "TAC": 0.56, "TGC": 0.54,
          "TTA": 0.08, "TCA": 0.15, "TAA": 0.30, "TGA": 0.47,
          "TTG": 0.13, "TCG": 0.05, "TAG": 0.24, "TGG": 1.00,
          "CTT": 0.13, "CCT": 0.29, "CAT": 0.42, "CGT": 0.08,
          "CTC": 0.20, "CCC": 0.32, "CAC": 0.58, "CGC": 0.18,
          "CTA": 0.07, "CCA": 0.28, "CAA": 0.27, "CGA": 0.11,
          "CTG": 0.40, "CCG": 0.11, "CAG": 0.73, "CGG": 0.20,
          "ATT": 0.36, "ACT": 0.25, "AAT": 0.47, "AGT": 0.15,
          "ATC": 0.47, "ACC": 0.36, "AAC": 0.53, "AGC": 0.24,
          "ATA": 0.17, "ACA": 0.28, "AAA": 0.43, "AGA": 0.21,
          "ATG": 1.00, "ACG": 0.11, "AAG": 0.57, "AGG": 0.21,
          "GTT": 0.18, "GCT": 0.27, "GAT": 0.46, "GGT": 0.16,
          "GTC": 0.24, "GCC": 0.40, "GAC": 0.54, "GGC": 0.34,
          "GTA": 0.12, "GCA": 0.23, "GAA": 0.42, "GGA": 0.25,
          "GTG": 0.46, "GCG": 0.11, "GAG": 0.58, "GGG": 0.25}
    SeqCai.set_cai_index(cd)
    for seq in SeqIO.parse(file, "fasta"):
        cai = SeqCai.cai_for_gene(str(seq.seq))
        idt = seq.id[0:-4]  # To exclude the _Coding suffix.
        caidf.loc[idt] = [cai]
    return caidf


def predict_binding(ffile, motifs):
    """Method to run a FIMO search and parse and collect the results.

    Requires the intallation of the MEME suite."""
    # TODO implement the method!!!
    subprocess.call('fimo --verbosity 1 ' + motifs + ' ' + ffile.name, shell=True)
    fimo_tab = pd.read_csv("fimo_out/fimo.tsv", sep="\t")
    fimo_tab = fimo_tab.reset_index(drop=True)
    return None


def txt2fasta(cdna_feat_table, fastaOut):
    """Write the ENSEMBL features to a fasta file with the appropriate first fasta line."""
    with fastaOut as ff:
        for i, r in cdna_feat_table.iterrows():
            ff.write(">{} |GeneID:{}|GeneName:{}|cDNA_start:{}|cDNA_end:{}|TSL:{}|APPRIS:{}|Source:{}|{}\n".format(i, r["Gene stable ID"], r["Gene name"], r["cDNA coding start"], r["cDNA coding end"], r["Transcript support level (TSL)"], r["APPRIS annotation"], r["Source (transcript)"], r["Gene description"]))
            ff.write("{}\n".format(r["cDNA sequences"]))


def chunks(l, n):
    """Yield successive n-sized chunks from a list l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]



## OBSOLETE functions
def get_utr5MAX_OBSOLETE(cdna_feat_row):
    """Select the longest 5'UTR from a cdna_feat-row with multiples utrs."""
    utr5s_start = cdna_feat_row["5' UTR start"].values[0].split(";")
    utr5s_end = cdna_feat_row["5' UTR end"].values[0].split(";")
    size_liste = []
    for i in range(len(utr5s_start)):
        size = int(utr5s_end[i])-int(utr5s_start[i])
        size_liste.append(size)
    indice_max = size_liste.index(max(size_liste))
    max_utr5_start = int(utr5s_start[indice_max])
    max_utr5_end = int(utr5s_end[indice_max])
    return([max_utr5_start, max_utr5_end])


def get_utr3MAX_OBSOLETE(cdna_feat_row):
    """Select the longest 3'UTR from a cdna_feat-row with multiples utrs."""
    utr3s_start = cdna_feat_row["3' UTR start"].values[0].split(";")
    utr3s_end = cdna_feat_row["3' UTR end"].values[0].split(";")
    size_liste = []
    for i in range(len(utr3s_start)):
        size = int(utr3s_end[i])-int(utr3s_start[i])
        size_liste.append(size)
    indice_max = size_liste.index(max(size_liste))
    max_utr3_start = int(utr3s_start[indice_max])
    max_utr3_end = int(utr3s_end[indice_max])
    return([max_utr3_start, max_utr3_end])


def get_cDNAstartMIN_OBSOLETE(cdna_feat_row):
    """Select the minimum cdna_start feature from multiples cDNA starts."""
    cDNA_start = cdna_feat_row["cDNA coding start"].values[0].split(";")
    min_cDNA_start = min(map(int, cDNA_start))
    return min_cDNA_start


def get_cDNAendMAX_OBSOLETE(cdna_feat_row):
    """Select the maximum cdna_end feature from multiples cDNA ends."""
    cDNA_end = cdna_feat_row["cDNA coding end"].values[0].split(";")
    max_cDNA_end = max(map(int, cDNA_end))
    return max_cDNA_end
