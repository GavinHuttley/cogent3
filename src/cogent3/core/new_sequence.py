"""Contains classes that represent biological sequence data.

Notes
-----
These should be created via MolType.make_seq()
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import typing
import warnings

from abc import ABC, abstractmethod
from collections import defaultdict
from functools import singledispatch, total_ordering
from operator import eq, ne
from random import shuffle

from numpy import array, floating, integer, issubdtype

from cogent3._version import __version__
from cogent3.core import new_alphabet
from cogent3.core.annotation import Feature
from cogent3.core.annotation_db import (
    BasicAnnotationDb,
    FeatureDataType,
    GenbankAnnotationDb,
    SupportsFeatures,
    load_annotations,
)
from cogent3.core.info import Info as InfoClass
from cogent3.core.location import FeatureMap, IndelMap, LostSpan
from cogent3.format.fasta import alignment_to_fasta
from cogent3.maths.stats.contingency import CategoryCounts
from cogent3.maths.stats.number import CategoryCounter
from cogent3.util.dict_array import DictArrayTemplate
from cogent3.util.misc import (
    DistanceFromMatrix,
    get_object_provenance,
    get_setting_from_environ,
)
from cogent3.util.transform import for_seq, per_shortest


OptGeneticCodeType = typing.Optional[typing.Union[int, str, "GeneticCode"]]
OptStr = typing.Optional[str]
OptInt = typing.Optional[int]
StrORIterableStr = typing.Union[str, typing.Iterable[str]]
ARRAY_TYPE = type(array(1))

# standard distance functions: left  because generally useful
frac_same = for_seq(f=eq, aggregator=sum, normalizer=per_shortest)
frac_diff = for_seq(f=ne, aggregator=sum, normalizer=per_shortest)


def _is_int(val) -> bool:
    """whether val is builtin, or numpy, integer"""
    return issubdtype(val.__class__, integer) or isinstance(val, int)


def _is_float(val) -> bool:
    """whether val is builtin, or numpy, integer"""
    return issubdtype(val.__class__, floating) or isinstance(val, float)


@total_ordering
class Sequence:
    """Holds the standard Sequence object. Immutable.

    Notes
    -----
    Sequences should be constructed by a MolType instance.
    """

    def __init__(
        self,
        moltype: "MolType",
        seq: str,
        *,
        name: OptStr = None,
        info: typing.Optional[typing.Union[dict, InfoClass]] = None,
        annotation_offset: int = 0,
    ):
        """Initialize a sequence.

        Parameters
        ----------
        moltype
            MolType instance
        seq
            the raw sequence string, default is ''
        name
            the sequence name
        info
            Info object or dict
        annotation_offset
            integer indicating start position relative to annotations
        """
        self.moltype = moltype
        self.name = name
        self._seq = _coerce_to_seqview(seq, name, self.moltype.most_degen_alphabet())
        info = info or {}
        self.info = InfoClass(**info)
        self._repr_policy = dict(num_pos=60)
        self._annotation_db = BasicAnnotationDb()
        self.annotation_offset = annotation_offset

    def __str__(self):
        return str(self._seq)

    def __bytes__(self):
        return bytes(self._seq)

    def __array__(self):
        return array(self._seq)

    def to_fasta(self, make_seqlabel=None, block_size=60):
        """Return string of self in FASTA format, no trailing newline

        Parameters
        ----------
        make_seqlabel
            callback function that takes the seq object and
            returns a label str

        """

        label = "0"

        if make_seqlabel is not None:
            label = make_seqlabel(self)
        elif hasattr(self, "label") and self.label:
            label = self.label
        elif hasattr(self, "name") and self.name:
            label = self.name
        return alignment_to_fasta({label: str(self)}, block_size=block_size)

    def to_rich_dict(self, exclude_annotations=False):
        """returns {'name': name, 'seq': sequence, 'moltype': moltype.label}"""
        info = {} if self.info is None else self.info
        if info.get("Refs", None) is not None and "Refs" in info:
            info.pop("Refs")

        info = info or None
        seq = self._seq.to_rich_dict() if hasattr(self, "_seq") else str(self)
        data = dict(
            name=self.name,
            seq=seq,
            moltype=self.moltype.label,
            info=info,
            type=get_object_provenance(self),
            version=__version__,
        )
        if hasattr(self, "annotation_offset"):
            offset = int(self._seq.parent_start)
            data.update(dict(annotation_offset=offset))

        if (
            hasattr(self, "annotation_db")
            and self.annotation_db
            and not exclude_annotations
        ):
            data["annotation_db"] = self.annotation_db.to_rich_dict()

        return data

    def to_json(self):
        """returns a json formatted string"""
        return json.dumps(self.to_rich_dict())

    def count(self, item):
        """count() delegates to self._seq."""
        return str(self).count(item)

    def counts(
        self,
        motif_length=1,
        include_ambiguity=False,
        allow_gap=False,
        exclude_unobserved=False,
        warn=False,
    ):
        """returns dict of counts of motifs

        only non-overlapping motifs are counted.

        Parameters
        ----------
        motif_length
            number of elements per character.
        include_ambiguity
            if True, motifs containing ambiguous characters
            from the seq moltype are included. No expansion of those is attempted.
        allow_gaps
            if True, motifs containing a gap character are included.
        exclude_unobserved
            if True, unobserved motif combinations are excluded.
        warn
            warns if motif_length > 1 and alignment trimmed to produce
            motif columns
        """
        try:
            data = str(self)
        except AttributeError:
            data = self._data

        not_array = isinstance(data, (SeqView, str))

        if motif_length == 1:
            counts = CategoryCounter(data)
        else:
            if warn and len(data) % motif_length != 0:
                warnings.warn(
                    f"{self.name} length not divisible by {motif_length}, truncating"
                )
            limit = (len(data) // motif_length) * motif_length
            data = data[:limit]
            if not_array:
                counts = CategoryCounter(
                    data[i : i + motif_length] for i in range(0, limit, motif_length)
                )
            else:
                counts = CategoryCounter(
                    tuple(v) for v in data.reshape(limit // motif_length, motif_length)
                )
        if not not_array:
            for key in list(counts):
                indices = [key] if motif_length == 1 else key
                motif = self.alphabet.to_chars(indices).astype(str)
                motif = "".join(motif)
                counts[motif] = counts.pop(key)

        exclude = []
        if not include_ambiguity or not allow_gap:
            is_degen = self.moltype.is_degenerate
            is_gap = self.moltype.is_gapped
            for motif in counts:
                if not include_ambiguity and is_degen(motif):
                    exclude.append(motif)
                elif not allow_gap and is_gap(motif):
                    exclude.append(motif)

        for motif in exclude:
            del counts[motif]

        return counts

    def __lt__(self, other):
        """compares based on the sequence string."""
        return str(self) < str(other)

    def __eq__(self, other):
        """compares based on the sequence string."""
        return str(self) == str(other)

    def __ne__(self, other):
        """compares based on the sequence string."""
        return str(self) != str(other)

    def __hash__(self):
        """__hash__ behaves like the sequence string for dict lookup."""
        return hash(str(self))

    def __contains__(self, other):
        """__contains__ checks whether other is in the sequence string."""
        return other in str(self)

    def shuffle(self):
        """returns a randomized copy of the Sequence object"""
        randomized_copy_list = list(self)
        shuffle(randomized_copy_list)
        return self.__class__(
            moltype=self.moltype, seq="".join(randomized_copy_list), info=self.info
        )

    def strip_degenerate(self):
        """Removes degenerate bases by stripping them out of the sequence."""
        return self.__class__(
            moltype=self.moltype,
            seq=self.moltype.strip_degenerate(self),
            info=self.info,
        )

    def strip_bad(self):
        """Removes any symbols not in the alphabet."""
        return self.__class__(
            moltype=self.moltype, seq=self.moltype.strip_bad(self), info=self.info
        )

    def strip_bad_and_gaps(self):
        """Removes any symbols not in the alphabet, and any gaps."""
        return self.__class__(
            moltype=self.moltype,
            seq=self.moltype.strip_bad_and_gaps(self),
            info=self.info,
        )

    def is_gapped(self):
        """Returns True if sequence contains gaps."""
        return self.moltype.is_gapped(self)

    def is_gap(self, char=None):
        """Returns True if char is a gap.

        If char is not supplied, tests whether self is gaps only.
        """
        if char is None:  # no char - so test if self is all gaps
            return len(self) == self.count_gaps()
        else:
            return self.moltype.is_gap(char)

    def is_degenerate(self):
        """Returns True if sequence contains degenerate characters."""
        return self.moltype.is_degenerate(self)

    def is_valid(self):
        """Returns True if sequence contains no items absent from alphabet."""
        return self.moltype.is_valid(self)

    def is_strict(self):
        """Returns True if sequence contains only monomers."""
        return self.moltype.is_strict(self)

    def first_gap(self):
        """Returns the index of the first gap in the sequence, or None."""
        return self.moltype.first_gap(self)

    def first_degenerate(self):
        """Returns the index of first degenerate symbol in sequence, or None."""
        return self.moltype.first_degenerate(self)

    def first_invalid(self):
        """Returns the index of first invalid symbol in sequence, or None."""
        return self.moltype.first_invalid(self)

    def first_non_strict(self):
        """Returns the index of first non-strict symbol in sequence, or None."""
        return self.moltype.first_non_strict(self)

    def disambiguate(self, method="strip"):
        """Returns a non-degenerate sequence from a degenerate one.

        method can be 'strip' (deletes any characters not in monomers or gaps)
        or 'random'(assigns the possibilities at random, using equal
        frequencies).
        """
        return self.__class__(
            moltype=self.moltype,
            seq=self.moltype.disambiguate(self, method),
            info=self.info,
        )

    def degap(self):
        """Deletes all gap characters from sequence."""
        result = self.__class__(
            moltype=self.moltype,
            seq=self.moltype.degap(self),
            name=self.name,
            info=self.info,
        )
        result.annotation_db = self.annotation_db
        return result

    def gap_indices(self):
        """Returns list of indices of all gaps in the sequence, or []."""
        return self.moltype.gap_indices(self)

    def gap_vector(self):
        """Returns vector of True or False according to which pos are gaps."""
        return self.moltype.gap_vector(self)

    def gap_maps(self):
        """Returns dicts mapping between gapped and ungapped positions."""
        return self.moltype.gap_maps(self)

    def count_gaps(self):
        """Counts the gaps in the specified sequence."""
        return self.moltype.count_gaps(self)

    def count_degenerate(self):
        """Counts the degenerate bases in the specified sequence."""
        return self.moltype.count_degenerate(self)

    def possibilities(self):
        """Counts number of possible sequences matching the sequence.

        Uses self.degenerates to decide how many possibilities there are at
        each position in the sequence.
        """
        return self.moltype.possibilities(self)

    def mw(self, method="random", delta=None):  # refactor: type hint
        """Returns the molecular weight of (one strand of) the sequence.

        If the sequence is ambiguous, uses method (random or strip) to
        disambiguate the sequence.

        If delta is passed in, adds delta per strand (default is None, which
        uses the alphabet default. Typically, this adds 18 Da for terminal
        water. However, note that the default nucleic acid weight assumes
        5' monophosphate and 3' OH: pass in delta=18.0 if you want 5' OH as
        well.

        Note that this method only calculates the MW of the coding strand. If
        you want the MW of the reverse strand, add self.rc().mw(). DO NOT
        just multiply the MW by 2: the results may not be accurate due to
        strand bias, e.g. in mitochondrial genomes.
        """
        return self.moltype.mw(self, method, delta)

    def can_match(self, other):
        """Returns True if every pos in self could match same pos in other.

        Truncates at length of shorter sequence.
        gaps are only allowed to match other gaps.
        """
        return self.moltype.can_match(self, other)

    def can_mismatch(self, other):
        """Returns True if any position in self could mismatch with other.

        Truncates at length of shorter sequence.
        gaps are always counted as matches.
        """
        return self.moltype.can_mismatch(self, other)

    def must_match(self, other):
        """Returns True if all positions in self must match positions in other."""
        return self.moltype.must_match(self, other)

    def diff(self, other):
        """Returns number of differences between self and other.

        NOTE: truncates at the length of the shorter sequence. Case-sensitive.
        """
        return self.distance(other)

    def distance(self, other, function=None):
        """Returns distance between self and other using function(i,j).

        other must be a sequence.

        function should be a function that takes two items and returns a
        number. To turn a 2D matrix into a function, use
        cogent3.util.miscs.DistanceFromMatrix(matrix).

        NOTE: Truncates at the length of the shorter sequence.

        Note that the function acts on two _elements_ of the sequences, not
        the two sequences themselves (i.e. the behavior will be the same for
        every position in the sequences, such as identity scoring or a function
        derived from a distance matrix as suggested above). One limitation of
        this approach is that the distance function cannot use properties of
        the sequences themselves: for example, it cannot use the lengths of the
        sequences to normalize the scores as percent similarities or percent
        differences.

        If you want functions that act on the two sequences themselves, there
        is no particular advantage in making these functions methods of the
        first sequences by passing them in as parameters like the function
        in this method. It makes more sense to use them as standalone functions.
        The factory function cogent3.util.transform.for_seq is useful for
        converting per-element functions into per-sequence functions, since it
        takes as parameters a per-element scoring function, a score aggregation
        function, and a normalization function (which itself takes the two
        sequences as parameters), returning a single function that combines
        these functions and that acts on two complete sequences.
        """
        if function is None:
            # use identity scoring function
            function = lambda a, b: a != b
        distance = 0
        for first, second in zip(self, other):
            distance += function(first, second)
        return distance

    def matrix_distance(self, other, matrix):
        """Returns distance between self and other using a score matrix.

        WARNING: the matrix must explicitly contain scores for the case where
        a position is the same in self and other (e.g. for a distance matrix,
        an identity between U and U might have a score of 0). The reason the
        scores for the 'diagonals' need to be passed explicitly is that for
        some kinds of distance matrices, e.g. log-odds matrices, the 'diagonal'
        scores differ from each other. If these elements are missing, this
        function will raise a KeyError at the first position that the two
        sequences are identical.
        """
        return self.distance(other, DistanceFromMatrix(matrix))

    def frac_same(self, other):
        """Returns fraction of positions where self and other are the same.

        Truncates at length of shorter sequence.
        Note that frac_same and frac_diff are both 0 if one sequence is empty.
        """
        return frac_same(self, other)

    def frac_diff(self, other):
        """Returns fraction of positions where self and other differ.

        Truncates at length of shorter sequence.
        Note that frac_same and frac_diff are both 0 if one sequence is empty.
        """
        return frac_diff(self, other)

    def frac_same_gaps(self, other):
        """Returns fraction of positions where self and other share gap states.

        In other words, if self and other are both all gaps, or both all
        non-gaps, or both have gaps in the same places, frac_same_gaps will
        return 1.0. If self is all gaps and other has no gaps, frac_same_gaps
        will return 0.0. Returns 0 if one sequence is empty.

        Uses self's gap characters for both sequences.
        """
        if not self or not other:
            return 0.0

        is_gap = self.moltype.gaps.__contains__
        return sum([is_gap(i) == is_gap(j) for i, j in zip(self, other)]) / min(
            len(self), len(other)
        )

    def frac_diff_gaps(self, other):
        """Returns frac. of positions where self and other's gap states differ.

        In other words, if self and other are both all gaps, or both all
        non-gaps, or both have gaps in the same places, frac_diff_gaps will
        return 0.0. If self is all gaps and other has no gaps, frac_diff_gaps
        will return 1.0.

        Returns 0 if one sequence is empty.

        Uses self's gap characters for both sequences.
        """
        if not self or not other:
            return 0.0
        return 1.0 - self.frac_same_gaps(other)

    def frac_same_non_gaps(self, other):
        """Returns fraction of non-gap positions where self matches other.

        Doesn't count any position where self or other has a gap.
        Truncates at the length of the shorter sequence.

        Returns 0 if one sequence is empty.
        """
        # refactor: simplify
        # refactor: array - make use of self._seq.array_value
        if not self or not other:
            return 0.0

        is_gap = self.moltype.gaps.__contains__
        count = 0
        identities = 0
        for i, j in zip(self, other):
            if is_gap(i) or is_gap(j):
                continue
            count += 1
            if i == j:
                identities += 1

        if count:
            return identities / count
        else:  # there were no positions that weren't gaps
            return 0

    def frac_diff_non_gaps(self, other):
        """Returns fraction of non-gap positions where self differs from other.

        Doesn't count any position where self or other has a gap.
        Truncates at the length of the shorter sequence.

        Returns 0 if one sequence is empty. Note that this means that
        frac_diff_non_gaps is _not_ the same as 1 - frac_same_non_gaps, since both
        return 0 if one sequence is empty.
        """
        if not self or not other:
            return 0.0

        is_gap = self.moltype.gaps.__contains__
        count = 0
        diffs = 0
        for i, j in zip(self, other):
            if is_gap(i) or is_gap(j):
                continue
            count += 1
            if i != j:
                diffs += 1

        if count:
            return diffs / count
        else:  # there were no positions that weren't gaps
            return 0

    def frac_similar(self, other, similar_pairs):
        """Returns fraction of positions where self[i] is similar to other[i].

        similar_pairs must be a dict such that d[(i,j)] exists if i and j are
        to be counted as similar. Use PairsFromGroups in cogent3.util.misc to
        construct such a dict from a list of lists of similar residues.

        Truncates at the length of the shorter sequence.

        Note: current implementation re-creates the distance function each
        time, so may be expensive compared to creating the distance function
        using for_seq separately.

        Returns 0 if one sequence is empty.
        """
        if not self or not other:
            return 0.0

        return for_seq(f=lambda x, y: (x, y) in similar_pairs, normalizer=per_shortest)(
            self, other
        )

    def with_termini_unknown(self):
        """Returns copy of sequence with terminal gaps remapped as missing."""
        gaps = self.gap_vector()
        first_nongap = last_nongap = None
        for i, state in enumerate(gaps):
            if not state:
                if first_nongap is None:
                    first_nongap = i
                last_nongap = i
        missing = self.moltype.missing
        if first_nongap is None:  # sequence was all gaps
            result = self.__class__(
                moltype=self.moltype, seq=[missing for _ in len(self)], info=self.info
            )
        else:
            prefix = missing * first_nongap
            mid = str(self)[first_nongap : last_nongap + 1]
            suffix = missing * (len(self) - last_nongap - 1)
            result = self.__class__(
                moltype=self.moltype, seq=prefix + mid + suffix, info=self.info
            )
        return result

    def _repr_html_(self):
        settings = self._repr_policy.copy()
        env_vals = get_setting_from_environ(
            "COGENT3_ALIGNMENT_REPR_POLICY",
            dict(num_pos=int),
        )
        settings.update(env_vals)
        return self.to_html(limit=settings["num_pos"])

    def to_html(
        self,
        wrap=60,
        limit=None,
        colors=None,
        font_size=12,
        font_family="Lucida Console",
    ):
        """returns html with embedded styles for sequence colouring

        Parameters
        ----------
        wrap
            maximum number of printed bases, defaults to
            alignment length
        limit
            truncate alignment to this length
        colors
            {character
            moltype.
        font_size
            in points. Affects labels and sequence and line spacing
            (proportional to value)
        font_family
            string denoting font family

        To display in jupyter notebook:

            >>> from IPython.core.display import HTML
            >>> HTML(aln.to_html())
        """
        css, styles = self.moltype.get_css_style(
            colors=colors, font_size=font_size, font_family=font_family
        )

        seq = str(self)
        seq = seq if limit is None else seq[:limit]
        gaps = "".join(self.moltype.gaps)
        seqlen = len(seq)
        start_gap = re.search(f"^[{gaps}]+", "".join(seq))
        end_gap = re.search(f"[{gaps}]+$", "".join(seq))

        start = 0 if start_gap is None else start_gap.end()
        end = len(seq) if end_gap is None else end_gap.start()
        seq_style = []
        template = '<span class="%s">%%s</span>'
        styled_seq = []
        for i in range(seqlen):
            char = seq[i]
            if i < start or i >= end:
                style = f"terminal_ambig_{self.moltype.label}"
            else:
                style = styles[char]

            seq_style.append(template % style)
            styled_seq.append(seq_style[-1] % char)

        # make a html table
        seq = array(styled_seq, dtype="O")
        table = ["<table>"]
        seq_ = "<td>%s</td>"
        label_ = '<td class="label">%s</td>'
        num_row_ = '<tr class="num_row"><td></td><td><b>{:,d}</b></td></tr>'
        for i in range(0, seqlen, wrap):
            table.append(num_row_.format(i))
            seqblock = seq[i : i + wrap].tolist()
            seqblock = "".join(seqblock)
            row = "".join([label_ % self.name, seq_ % seqblock])
            table.append(f"<tr>{row}</tr>")
        table.append("</table>")
        class_name = self.__class__.__name__
        if limit and limit < len(self):
            summary = f"{class_name}, length={len(self):,} (truncated to {limit if limit else len(self)})"
        else:
            summary = f"{class_name}, length={len(self):,}"

        text = [
            "<style>",
            ".c3seq table {margin: 10px 0;}",
            ".c3seq td { border: none !important; text-align: left !important; }",
            ".c3seq tr:not(.num_row) td span {margin: 0 2px;}",
            ".c3seq tr:nth-child(even) {background: #f7f7f7;}",
            ".c3seq .num_row {background-color:rgba(161, 195, 209, 0.5) !important; border-top: solid 1px black; }",
            ".c3seq .label { font-size: %dpt ; text-align: right !important; "
            "color: black !important; padding: 0 4px; }" % font_size,
            "\n".join([".c3seq " + style for style in css]),
            "</style>",
            '<div class="c3seq">',
            "\n".join(table),
            f"<p><i>{summary}</i></p>",
            "</div>",
        ]
        return "\n".join(text)

    def __add__(self, other):
        """Adds two sequences (other can be a string as well)."""
        if hasattr(other, "moltype"):
            if self.moltype != other.moltype:
                raise ValueError(
                    f"MolTypes don't match: ({self.moltype},{other.moltype})"
                )
        other_seq = str(other)

        # If two sequences with the same name are being added together the name should not be None
        if type(other) == type(self):
            name = self.name if self.name == other.name else None
        else:
            name = None

        return self.__class__(
            moltype=self.moltype, seq=str(self) + other_seq, name=name
        )

    @property
    def annotation_offset(self):
        """
        The offset between annotation coordinates and sequence coordinates.

        The offset can be used to adjust annotation coordinates to match the position
        of the given Sequence within a larger genomic context. For example, if the
        Annotations are with respect to a chromosome, and the sequence represents
        a gene that is 100 bp from the start of a chromosome, the offset can be set to
        100 to ensure that the gene's annotations are aligned with the appropriate
        genomic positions.


        Returns:
            int: The offset between annotation coordinates and sequence coordinates.
        """

        return self._seq.parent_start

    @annotation_offset.setter
    def annotation_offset(self, value):
        self._seq.offset = value

    @property
    def annotation_db(self):
        return self._annotation_db

    @annotation_db.setter
    def annotation_db(self, value):
        # Without knowing the contents of the db we cannot
        # establish whether self.moltype is compatible, so
        # we rely on the user to get that correct
        # one approach to support validation might be to add
        # to the SupportsFeatures protocol a is_nucleic flag,
        # for both DNA and RNA. But if a user trys get_slice()
        # on a '-' strand feature, they will get a TypError.
        # I think that's enough.
        self.replace_annotation_db(value, check=False)

    def replace_annotation_db(
        self, value: SupportsFeatures, check: bool = True
    ) -> None:
        """public interface to assigning the annotation_db

        Parameters
        ----------
        value
            the annotation db instance
        check
            whether to check value supports the feature interface

        Notes
        -----
        The check can be very expensive, so if you're confident set it to False
        """
        if value == self._annotation_db:
            return

        if check and value:
            if not isinstance(value, SupportsFeatures):
                raise TypeError(f"{type(value)} does not satisfy SupportsFeatures")

        self._annotation_db = value

    def get_features(
        self,
        *,
        biotype: OptStr = None,
        name: OptStr = None,
        start: OptInt = None,
        stop: OptInt = None,
        allow_partial: bool = False,
    ):
        """yields Feature instances

        Parameters
        ----------
        biotype
            biotype of the feature
        name
            name of the feature
        start, stop
            start, stop positions to search between, relative to offset
            of this sequence. If not provided, entire span of sequence is used.

        Notes
        -----
        When dealing with a nucleic acid moltype, the returned features will
        yield a sequence segment that is consistently oriented irrespective
        of strand of the current instance.
        """

        if self._annotation_db is None:
            return None

        start = start or 0
        stop = stop or len(self)

        # handle negative start / stop
        start = start + len(self) if start < 0 else start
        stop = stop + len(self) if stop < 0 else stop

        start, stop = (start, stop) if start < stop else (stop, start)

        # note: offset is handled by absolute_position
        # we set include_boundary=False because start is inclusive indexing,
        # i,e., the start cannot be equal to the length of the view
        (
            # parent_id can differ from self.name if there was a
            # rename operation
            parent_id,
            *_,
            strand,
        ) = self.parent_coordinates()
        query_start = self._seq.absolute_position(start, include_boundary=False)
        # we set include_boundary=True because stop is exclusive indexing,
        # i,e., the stop can be equal to the length of the view
        query_stop = self._seq.absolute_position(stop, include_boundary=True)

        rev_strand = strand == -1
        if rev_strand:
            query_start, query_stop = query_stop, query_start

        query_start = max(query_start, 0)
        # in the underlying db, features are always plus strand oriented
        # (need to check that for user defined features)
        # a '-' strand feature in the db may be [(2, 4), (6, 8)]
        # so we would take 7-6, 3-2 and complement it
        # if the current sequence is reverse complemented
        # then a '+' feature from the db needs to be nucleic reversed
        # and a '-' feature from the db needs to be nucleic reversed too

        # making this more complicated is self.make_feature() assumes
        # all coordinates are with respect to the current orientation
        # so self.make_feature(data(spans=[(2,4)])) has different meaning if
        # self is rc'ed, it would correspond to len(self)-2, etc...
        # To piggy-back on that method we need to convert our feature spans
        # into the current orientation. HOWEVER, we also have the reversed
        # flag which comes back from the db

        for feature in self.annotation_db.get_features_matching(
            seqid=parent_id,
            name=name,
            biotype=biotype,
            start=query_start,
            stop=query_stop,
            allow_partial=allow_partial,
        ):
            # spans need to be converted from absolute to relative positions
            # DO NOT do adjustment in make_feature since that's user facing,
            # and we expect them to make a feature manually wrt to their
            # current view
            spans = array(feature.pop("spans"), dtype=int)
            for i, v in enumerate(spans.ravel()):
                rel_pos = self._seq.relative_position(v)
                spans.ravel()[i] = rel_pos

            if rev_strand:
                # see above comment
                spans = len(self) - spans

            feature["spans"] = spans.tolist()
            yield self.make_feature(feature)

    def _relative_spans(self, spans):
        r_spans = []

        for s, e in spans:
            # reverse feature determined by absolute position
            reverse = s > e

            if reverse:
                start = self._seq.relative_position(s, stop=True)
                end = self._seq.relative_position(e)
            else:
                start = self._seq.relative_position(s)
                end = self._seq.relative_position(e, stop=True)

            r_spans += [(start, end)]

        return r_spans

    def make_feature(self, feature: FeatureDataType, *args) -> Feature:
        """
        return an Feature instance from feature data

        Parameters
        ----------
        feature
            dict of key data to make an Feature instance

        Notes
        -----
        Unlike add_feature(), this method does not add the feature to the
        database.
        We assume that spans represent the coordinates for this instance!
        """
        feature = dict(feature)
        seq_rced = self._seq.is_reversed
        spans = feature.pop("spans", None)
        revd = feature.pop("strand", None) == "-"
        feature["strand"] = "+" if revd == seq_rced else "-"

        vals = array(spans)
        pre = abs(vals.min()) if vals.min() < 0 else 0
        post = abs(vals.max() - len(self)) if vals.max() > len(self) else 0

        # we find the spans > len(self)
        new_spans = []
        for coord in vals:
            new = coord[:]
            if coord.min() < 0 < coord.max():
                new = coord[:]
                new[new < 0] = 0
            elif coord.min() < len(self) < coord.max():
                new[new > len(self)] = len(self)
            elif coord[0] == coord[1] or coord.min() > len(self) or coord.max() < 0:
                # would really to adjust pre and post to be up to the next span
                continue
            new_spans.append(new.tolist())

        fmap = FeatureMap.from_locations(locations=new_spans, parent_length=len(self))
        if pre or post:
            # create a lost span to represent the segment missing from
            # the instance
            spans = list(fmap.spans)
            if pre:
                spans.insert(0, LostSpan(pre))
            if post:
                spans.append(LostSpan(post))
            fmap = FeatureMap(spans=spans, parent_length=len(self))

        if seq_rced:
            fmap = fmap.nucleic_reversed()

        feature.pop("on_alignment", None)
        feature.pop("seqid", None)
        return Feature(parent=self, seqid=self.name, map=fmap, **feature)

    def annotate_from_gff(self, f: os.PathLike, offset=None):
        """copies annotations from a gff file to self,

        Parameters
        ----------
        f : path to gff annotation file.
        offset : Optional, the offset between annotation coordinates and sequence coordinates.
        """
        if isinstance(self.annotation_db, GenbankAnnotationDb):
            raise ValueError("GenbankAnnotationDb already attached")

        self.annotation_db = load_annotations(
            path=f, seqids=self.name, db=self.annotation_db
        )

        if offset:
            self.annotation_offset = offset

    def add_feature(
        self,
        *,
        biotype: str,
        name: str,
        spans: list[tuple[int, int]],
        parent_id: OptStr = None,
        strand: OptStr = None,
        on_alignment: bool = False,
        seqid: OptStr = None,
    ) -> Feature:
        """
        add a feature to annotation_db

        Parameters
        ----------
        biotype
            biological type
        name
            name of the feature
        spans
            coordinates for this sequence
        parent_id
            name of the feature parent
        strand
            '+' or '-', defaults to '+'
        on_alignment
            whether the feature spans are alignment coordinates
        seqid
            ignored since the feature is added to this sequence

        Returns
        -------
        Feature instance
        """
        feature_data = FeatureDataType(
            seqid=self.name,
            **{n: v for n, v in locals().items() if n not in ("self", "seqid")},
        )

        self.annotation_db.add_feature(**feature_data)
        for discard in ("on_alignment", "parent_id"):
            feature_data.pop(discard)
        return self.make_feature(feature_data)

    def to_moltype(self, moltype):
        """returns copy of self with moltype seq

        Parameters
        ----------
        moltype : str
            molecular type
        """
        from cogent3.core import new_moltype

        if not moltype:
            raise ValueError(f"unknown moltype '{moltype}'")

        moltype = new_moltype.get_moltype(moltype)
        if moltype is self.moltype:
            return self

        s = moltype.coerce_str(self._seq.value)
        moltype.verify_sequence(s, gaps_allowed=True, wildcards_allowed=True)
        sv = SeqView(seq=s, alphabet=moltype.most_degen_alphabet())
        new = moltype.make_seq(sv, name=self.name, info=self.info)
        new.annotation_db = self.annotation_db
        return new

    def _seq_filter(self, seq):
        """Returns filtered seq; used to do DNA/RNA conversions."""
        return seq

    def copy_annotations(self, seq_db: SupportsFeatures) -> None:
        """copy annotations into attached annotation db

        Parameters
        ----------
        seq_db
            compatible annotation db

        Notes
        -----
        Only copies annotations for records with seqid equal to self.name
        """
        if not isinstance(seq_db, SupportsFeatures):
            raise TypeError(
                f"type {type(seq_db)} does not match SupportsFeatures interface"
            )

        if not seq_db.num_matches(seqid=self.name):
            return

        if self.annotation_db and not self.annotation_db.compatible(seq_db):
            raise TypeError(f"type {type(seq_db)} != {type(self.annotation_db)}")

        if self.annotation_db is None:
            self.annotation_db = type(seq_db)()

        self.annotation_db.update(seq_db, seqids=self.name)

    def copy(self, exclude_annotations: bool = False, sliced: bool = True):
        """returns a copy of self

        Parameters
        -----------
        sliced
            Slices underlying sequence with start/end of self coordinates. The offset
            is retained.
        exclude_annotations
            drops annotation_db when True
        """
        annotation_offset = self.annotation_offset if sliced else self._seq.offset
        data = self._seq.copy(sliced=sliced)
        new = self.__class__(
            moltype=self.moltype,
            seq=data,
            name=self.name,
            info=self.info,
            annotation_offset=annotation_offset,
        )
        db = None if exclude_annotations else copy.deepcopy(self.annotation_db)
        new._annotation_db = db
        return new

    def with_masked_annotations(
        self, annot_types, mask_char=None, shadow=False, extend_query=False
    ):
        """returns a sequence with annot_types regions replaced by mask_char
        if shadow is False, otherwise all other regions are masked.

        Parameters
        ----------
        annot_types
            annotation type(s)
        mask_char
            must be a character valid for the seq MolType. The
            default value is the most ambiguous character, eg. '?' for DNA
        shadow
            whether to mask the annotated regions, or everything but
            the annotated regions
        """
        if mask_char is None:
            ambigs = [(len(v), c) for c, v in list(self.moltype.ambiguities.items())]
            ambigs.sort()
            mask_char = ambigs[-1][1]
        assert mask_char in self.moltype, f"Invalid mask_char {mask_char}"

        annotations = []
        annot_types = [annot_types] if isinstance(annot_types, str) else annot_types
        for annot_type in annot_types:
            annotations += list(
                self.get_features(biotype=annot_type, allow_partial=True)
            )

        if not annotations:
            region = Feature(
                parent=self,
                seqid=self.name,
                name=None,
                biotype=None,
                map=FeatureMap.from_locations(locations=[], parent_length=len(self)),
                strand="+",
            )
        else:
            region = annotations[0].union(annotations[1:])

        if shadow:
            region = region.shadow()

        i = 0
        segments = []
        coords = region.map.get_coordinates()
        for b, e in coords:
            segments.extend((str(self[i:b]), mask_char * (e - b)))
            i = e
        segments.append(str(self[i:]))

        new = self.__class__(
            moltype=self.moltype, seq="".join(segments), name=self.name, info=self.info
        )
        new.annotation_db = self.annotation_db
        return new

    def gapped_by_map_segment_iter(
        self, segment_map, allow_gaps=True, recode_gaps=False
    ) -> str:
        if not allow_gaps and not segment_map.complete:
            raise ValueError(f"gap(s) in map {segment_map}")

        for span in segment_map.spans:
            if span.lost:
                unknown = "?" if span.terminal or recode_gaps else "-"
                seg = unknown * span.length
            else:
                seg = str(self[span.start : span.end])

            yield seg

    def gapped_by_map_motif_iter(self, map):
        for segment in self.gapped_by_map_segment_iter(map):
            yield from segment

    def gapped_by_map(self, map, recode_gaps=False):
        segments = self.gapped_by_map_segment_iter(map, True, recode_gaps)
        return self.__class__(
            moltype=self.moltype,
            seq="".join(segments),
            name=self.name,
            info=self.info,
        )

    def _mapped(self, map):
        # Called by generic __getitem__
        segments = self.gapped_by_map_segment_iter(map, allow_gaps=False)
        return self.__class__(
            moltype=self.moltype, seq="".join(segments), name=self.name, info=self.info
        )

    def __repr__(self):
        myclass = f"{self.__class__.__name__}"
        myclass = myclass.split(".")[-1]
        seq = f"{str(self)[:7]}... {len(self):,}" if len(self) > 10 else str(self)
        return f"{myclass}({seq})"

    def __getitem__(self, index):
        preserve_offset = False
        if hasattr(index, "get_slice"):
            if index.parent is not self:
                raise ValueError("cannot slice Feature not bound to self")
            return index.get_slice()

        if hasattr(index, "map"):
            index = index.map

        if isinstance(index, (FeatureMap, IndelMap)):
            new = self._mapped(index)
            preserve_offset = True

        elif isinstance(index, slice) or _is_int(index):
            new = self.__class__(
                moltype=self.moltype,
                seq=self._seq[index],
                name=self.name,
                info=self.info,
            )
            stride = getattr(index, "step", 1) or 1
            preserve_offset = stride > 0

        if isinstance(index, (list, tuple)):
            raise TypeError("cannot slice using list or tuple")

        if self.annotation_db is not None and preserve_offset:
            new.replace_annotation_db(self.annotation_db, check=False)
            new.annotation_offset = self.annotation_offset

        if _is_float(index):
            raise TypeError("cannot slice using float")

        if hasattr(self, "_repr_policy"):
            new._repr_policy.update(self._repr_policy)

        return new

    def __iter__(self):
        yield from iter(str(self))

    def get_name(self):
        """Return the sequence name -- should just use name instead."""
        return self.name

    def __len__(self):
        return len(self._seq)

    def get_type(self):
        """Return the sequence type as moltype label."""
        return self.moltype.label

    def resolved_ambiguities(self) -> list[tuple[str]]:
        """Returns a list of tuples of strings."""
        ambigs = self.moltype.ambiguities
        return [ambigs[motif] for motif in self._seq]

    def iter_kmers(self, k: int, strict: bool = True) -> typing.Iterator[str]:
        """generates all overlapping k-mers.
        When strict is True, the characters in the k-mer must be
        a subset of the canonical characters for the moltype"""
        if k <= 0:
            raise ValueError(f"k must be an int > 0, not {k}")

        if not isinstance(k, int):
            raise ValueError(f"k must be an int, not {k}")

        canonical = set(self.moltype)
        seq = str(self)
        for i in range(len(seq) - k + 1):
            kmer = seq[i : i + k]
            if not strict:
                yield kmer
            else:
                if all(char in canonical for char in kmer):
                    yield kmer

    def get_kmers(self, k: int, strict: bool = True) -> list[str]:
        """return all overlapping k-mers"""
        return list(self.iter_kmers(k, strict))

    def sliding_windows(self, window, step, start=None, end=None):
        """Generator function that yield new sequence objects
        of a given length at a given interval.

        Parameters
        ----------
        window
            The length of the returned sequence
        step
            The interval between the start of the returned
            sequence objects
        start
            first window start position
        end
            last window start position

        """
        start = [start, 0][start is None]
        end = [end, len(self) - window + 1][end is None]
        end = min(len(self) - window + 1, end)
        if start < end and len(self) - end >= window - 1:
            for pos in range(start, end, step):
                yield self[pos : pos + window]

    def get_in_motif_size(self, motif_length=1, warn=False):
        """returns sequence as list of non-overlapping motifs

        Parameters
        ----------
        motif_length
            length of the motifs
        warn
            whether to notify of an incomplete terminal motif
        """
        seq = self._seq
        if isinstance(seq, SeqView):
            seq = str(self)
        if motif_length == 1:
            return seq

        length = len(seq)
        remainder = length % motif_length
        if remainder and warn:
            warnings.warn(
                f'Dropped remainder "{seq[-remainder:]}" from end of sequence'
            )
        return [
            seq[i : i + motif_length]
            for i in range(0, length - remainder, motif_length)
        ]

    def parse_out_gaps(self):
        """returns Map corresponding to gap locations and ungapped Sequence"""
        gap = re.compile(f"[{re.escape(self.moltype.gap)}]+")
        seq = str(self)
        gap_pos = []
        cum_lengths = []
        for match in gap.finditer(seq):
            pos = match.start()
            gap_pos.append(pos)
            cum_lengths.append(match.end() - pos)

        gap_pos = array(gap_pos)
        cum_lengths = array(cum_lengths).cumsum()
        gap_pos[1:] = gap_pos[1:] - cum_lengths[:-1]

        seq = self.__class__(
            moltype=self.moltype,
            seq=gap.sub("", seq),
            name=self.get_name(),
            info=self.info,
        )
        indel_map = IndelMap(
            gap_pos=gap_pos, cum_gap_lengths=cum_lengths, parent_length=len(seq)
        )
        seq.annotation_db = self.annotation_db
        return indel_map, seq

    def replace(self, oldchar, newchar):
        """return new instance with oldchar replaced by newchar"""
        new = self._seq.replace(oldchar, newchar)
        result = self.__class__(
            moltype=self.moltype, seq=new, name=self.name, info=self.info
        )
        result.annotation_db = self.annotation_db
        return result

    def is_annotated(self):
        """returns True if sequence has any annotations"""
        num = self.annotation_db.num_matches() if self.annotation_db else 0
        return num != 0

    def annotate_matches_to(
        self, pattern: str, biotype: str, name: str, allow_multiple: bool = False
    ):
        """Adds an annotation at sequence positions matching pattern.

        Parameters
        ----------
        pattern
            The search string for which annotations are made. IUPAC ambiguities
            are converted to regex on sequences with the appropriate MolType.
        biotype
            The type of the annotation (e.g. "domain").
        name
            The name of the annotation.
        allow_multiple
            If True, allows multiple occurrences of the input pattern. Otherwise,
            only the first match is used.

        Returns
        -------
        Returns a list of Feature instances.
        """
        try:
            pattern = self.moltype.to_regex(seq=pattern)
        except ValueError:
            # assume already a regex
            pass

        pos = [m.span() for m in re.finditer(pattern, str(self))]
        if not pos:
            return []

        num_match = len(pos) if allow_multiple else 1
        return [
            self.add_feature(
                biotype=biotype,
                name=f"{name}:{i}" if allow_multiple else name,
                spans=[pos[i]],
            )
            for i in range(num_match)
        ]

    def get_drawables(
        self, *, biotype: typing.Optional[str, typing.Iterable[str]] = None
    ) -> dict:
        """returns a dict of drawables, keyed by type

        Parameters
        ----------
        biotype
            passed to get_features(biotype). Can be a single biotype or
            series. Only features matching this will be included.
        """
        # make sure the drawables are unique by adding to a set
        features = set(self.get_features(biotype=biotype, allow_partial=True))
        result = defaultdict(list)
        for f in features:
            result[f.biotype].append(f.get_drawable())
        return result

    def get_drawable(
        self,
        *,
        biotype: typing.Optional[StrORIterableStr] = None,
        width: int = 600,
        vertical: bool = False,
    ):
        """make a figure from sequence features

        Parameters
        ----------
        biotype
            passed to get_features(biotype). Can be a single biotype or
            series. Only features matching this will be included.
        width
            width in pixels
        vertical
            rotates the drawable

        Returns
        -------
        a Drawable instance

        Notes
        -----
        If provided, the biotype is used for plot order.
        """
        from cogent3.draw.drawable import Drawable

        drawables = self.get_drawables(biotype=biotype)
        if not drawables:
            return None

        biotype = list(drawables) if biotype is None else biotype
        biotypes = (biotype,) if isinstance(biotype, str) else biotype

        # we order by tracks
        top = 0
        space = 0.25
        annotes = []
        for feature_type in biotypes:
            new_bottom = top + space
            for i, annott in enumerate(drawables[feature_type]):
                annott.shift(y=new_bottom - annott.bottom)
                if i > 0:
                    annott._showlegend = False
                annotes.append(annott)

            top = annott.top

        top += space
        height = max((top / len(self)) * width, 300)
        xaxis = dict(range=[0, len(self)], zeroline=False, showline=True)
        yaxis = dict(range=[0, top], visible=False, zeroline=True, showline=True)

        if vertical:
            all_traces = [t.T.as_trace() for t in annotes]
            width, height = height, width
            xaxis, yaxis = yaxis, xaxis
        else:
            all_traces = [t.as_trace() for t in annotes]

        drawer = Drawable(
            title=self.name, traces=all_traces, width=width, height=height
        )
        drawer.layout.update(xaxis=xaxis, yaxis=yaxis)
        return drawer

    def parent_coordinates(self) -> tuple[str, int, int, int]:
        """returns seqid, start, stop, strand of this sequence on its parent

        Notes
        -----
        seqid is the identifier of the parent. Returned coordinates are with
        respect to the plus strand, irrespective of whether the sequence has
        been reversed complemented or not.

        Returns
        -------
        seqid, start, end, strand of this sequence on the parent. strand is either
        -1 or 1.
        """
        strand = -1 if self._seq.is_reversed else 1
        return self._seq.seqid, self._seq.parent_start, self._seq.parent_stop, strand


class ProteinSequence(Sequence):
    """Holds the standard Protein sequence."""

    # constructed by PROTEIN moltype
    ...


class ByteSequence(Sequence):
    """Holds the bytes sequence."""

    # constructed by BYTES moltype
    ...


class ProteinWithStopSequence(Sequence):
    """Holds the standard Protein sequence, allows for stop codon."""

    # constructed by PROTEIN_WITH_STOP moltype
    ...


class NucleicAcidSequenceMixin:
    """Mixin class for DNA and RNA sequences."""

    def __str__(self):
        result = str(self._seq)
        if self._seq.is_reversed:
            with contextlib.suppress(TypeError):
                result = self.moltype.complement(result)
        return result

    def can_pair(self, other):
        """Returns True if self and other could pair.

        Pairing occurs in reverse order, i.e. last position of other with
        first position of self, etc.

        Truncates at length of shorter sequence.
        gaps are only allowed to pair with other gaps, and are counted as 'weak'
        (same category as GU and degenerate pairs).

        NOTE: second must be able to be reverse
        """
        return self.moltype.can_pair(self, other)

    def can_mispair(self, other):
        """Returns True if any position in self could mispair with other.

        Pairing occurs in reverse order, i.e. last position of other with
        first position of self, etc.

        Truncates at length of shorter sequence.
        gaps are always counted as possible mispairs, as are weak pairs like GU.
        """
        return self.moltype.can_mispair(self, other)

    def must_pair(self, other):
        """Returns True if all positions in self must pair with other.

        Pairing occurs in reverse order, i.e. last position of other with
        first position of self, etc.
        """
        return not self.moltype.can_mispair(self, other)

    def complement(self):
        """Returns complement of self, using data from MolType.

        Always tries to return same type as item: if item looks like a dict,
        will return list of keys.
        """
        return self.__class__(
            moltype=self.moltype, seq=self.moltype.complement(self), info=self.info
        )

    def reverse_complement(self):
        """Converts a nucleic acid sequence to its reverse complement.
        Synonymn for rc."""
        return self.rc()

    def rc(self):
        """Converts a nucleic acid sequence to its reverse complement."""
        rc = self.__class__(
            moltype=self.moltype,
            seq=self._seq[::-1],
            name=self.name,
            info=self.info,
        )
        rc.annotation_db = self.annotation_db
        return rc

    def has_terminal_stop(
        self,
        gc: OptGeneticCodeType = None,
        strict: bool = False,
    ) -> bool:
        """Return True if the sequence has a terminal stop codon.

        Parameters
        ----------
        gc
            valid input to new_genetic_code.get_code(), a genetic code object, number
            or name
        strict
            If True, raises an exception if length not divisible by 3
        """
        from cogent3.core import new_genetic_code

        gc = new_genetic_code.get_code(gc)
        _, s = self.parse_out_gaps()

        divisible_by_3 = len(s) % 3 == 0
        if divisible_by_3:
            end3 = str(s[-3:])
            return gc.is_stop(end3)

        if strict:
            raise new_alphabet.AlphabetError(f"{self.name!r} length not divisible by 3")

        return False

    def trim_stop_codon(
        self,
        gc: OptGeneticCodeType = None,
        strict: bool = False,
    ):
        """Removes a terminal stop codon from the sequence

        Parameters
        ----------
        gc
            valid input to new_genetic_code.get_code(), a genetic code object, number
            or name
        strict
            If True, raises an exception if length not divisible by 3

        Notes
        -----
        If sequence contains gap characters, the result preserves the sequence
        length by adding gap characters at the end.
        """
        from cogent3.core import new_genetic_code

        if not self.has_terminal_stop(gc=gc, strict=strict):
            return self

        gc = new_genetic_code.get_code(gc)
        m, s = self.parse_out_gaps()

        divisible_by_3 = len(s) % 3 == 0
        if not divisible_by_3:
            return self

        end = str(s[-3:])

        if not gc.is_stop(end):
            return self

        if not m.num_gaps:
            # has zero length if no gaps
            return self[:-3]

        # determine terminal gap needed to fill in the sequence
        s = str(self)
        gaps = "".join(self.moltype.gaps)
        pattern = f"({'|'.join(gc['*'])})[{gaps}]*$"
        terminal_stop = re.compile(pattern)
        if match := terminal_stop.search(s):
            diff = len(s) - match.start()
            s = terminal_stop.sub("-" * diff, s)

        result = self.__class__(
            moltype=self.moltype, seq=s, name=self.name, info=self.info
        )
        result.annotation_db = self.annotation_db
        return result

    def get_translation(
        self,
        gc: OptGeneticCodeType = None,
        incomplete_ok: bool = False,
        include_stop: bool = False,
        trim_stop: bool = True,
    ):
        """translate to amino acid sequence

        Parameters
        ----------
        gc
            valid input to cogent3.get_code(), a genetic code object, number
            or name
        incomplete_ok
            codons that are mixes of nucleotide and gaps converted to '?'.
            raises a ValueError if False
        include_stop
            allows stop codons in translation
        trim_stop
            trims a terminal stop codon if it exists

        Returns
        -------
        sequence of PROTEIN moltype

        Raises
        ------
        AlphabetError if include_stop is False and a stop codon occurs
        """
        # todo gavin the genetic code should have the moltype for protein
        #  with stop. The genetic code should also do the translation off
        #  raw data, either an array of ints or a string. So this method
        #  should only deal with trimming terminal stops, modulo 3 etc...
        from cogent3.core import new_genetic_code, new_moltype

        protein = new_moltype.get_moltype(
            "protein_with_stop" if include_stop else "protein"
        )
        gc = new_genetic_code.get_code(gc)
        codon_alphabet = gc.get_alphabet(include_stop=include_stop).with_gap_motif()
        moltype = self.moltype
        # translate the codons
        translation = []
        if include_stop or not trim_stop:
            # we just deal with sequence as is
            seq = str(self)
        else:
            seq = str(self.trim_stop_codon(gc=gc, strict=not incomplete_ok))

        for posn in range(0, len(seq) - 2, 3):
            orig_codon = str(seq[posn : posn + 3])
            try:
                resolved = moltype.resolve_ambiguity(
                    orig_codon, alphabet=codon_alphabet
                )
            except new_alphabet.AlphabetError:
                if not incomplete_ok or "-" not in orig_codon:
                    raise new_alphabet.AlphabetError(
                        f"unresolvable codon {orig_codon!r} in {self.name}"
                    )
                resolved = (orig_codon,)
            trans = []
            for codon in resolved:
                if codon == "---":
                    aa = "-"
                elif "-" in codon:
                    aa = "?"
                    if not incomplete_ok:
                        raise new_alphabet.AlphabetError(
                            f"incomplete codon {codon} in {self.name}"
                        )
                else:
                    aa = gc[codon]
                    if aa == "*" and not include_stop:
                        continue
                trans.append(aa)
            if not trans:
                raise new_alphabet.AlphabetError(orig_codon)
            aa = protein.what_ambiguity(trans)
            translation.append(aa)

        return protein.make_seq(seq="".join(translation), name=self.name)

    def to_rna(self):
        """Returns copy of self as RNA."""
        return self.to_moltype("rna")

    def to_dna(self):
        """Returns copy of self as DNA."""
        return self.to_moltype("dna")

    def strand_symmetry(self, motif_length=1):
        """returns G-test for strand symmetry"""
        counts = self.counts(motif_length=motif_length)
        ssym_pairs = self.moltype.strand_symmetric_motifs(motif_length=motif_length)

        motifs = []
        obs = []
        for plus, minus in sorted(ssym_pairs):
            row = array([counts[plus], counts[minus]], dtype=int)
            if row.max() == 0:  # we ignore motifs missing on both strands
                continue

            obs.append(row)
            motifs.append(plus)

        template = DictArrayTemplate(motifs, ["+", "-"])
        obs = template.wrap(obs)
        cat = CategoryCounts(obs)
        return cat.G_fit()


def _input_vals_pos_step(seqlen, start, stop, step):
    start = 0 if start is None else start
    if start > 0 and start >= seqlen:
        # start beyond seq is an empty slice
        return 0, 0, 1

    stop = seqlen if stop is None else stop
    if stop < 0 and abs(stop) >= seqlen:
        # finished slice before we started seq!
        return 0, 0, 1

    start = max(seqlen + start, 0) if start < 0 else start

    if stop > 0:
        stop = min(seqlen, stop)
    elif stop < 0:
        stop += seqlen

    if start >= stop:
        start = stop = 0
        step = 1

    return start, stop, step


def _input_vals_neg_step(seqlen, start, stop, step):
    # Note how Python reverse slicing works
    # we need to make sure the start and stop are both
    # negative, for example "abcd"[-1:-5:-1] returns "dcba"
    if start is None or start >= seqlen:  # set default
        start = -1  # Done
    elif start >= 0:  # convert to -ve index
        start = start - seqlen
    elif start < -seqlen:  # start is bounded by len(seq)
        return 0, 0, 1

    if stop is None:  # set default
        stop = -seqlen - 1
    elif stop >= 0:
        stop -= seqlen

    stop = max(stop, -seqlen - 1)  # stop should always be <= len(seq)

    # checking for zero-length slice
    return (0, 0, 1) if start < stop else (start, stop, step)


class SliceRecordABC(ABC):
    """Abstract base class for recording the history of operations to be applied
    to some underlying data

    Notes
    -----
    seq_len refers to a Python typing.Sequence object, e.g. array, str, list.
    """

    __slots__ = ("start", "stop", "step", "_offset")

    @property
    @abstractmethod
    def seq_len(self) -> int: ...

    @abstractmethod
    def _get_init_kwargs(self) -> dict:
        """return required arguments for construction that are unique to the
        subclass"""
        ...

    @abstractmethod
    def copy(self): ...

    # refactor: design
    # can we remove the need for this method on the ABC and inheriting
    @property
    @abstractmethod
    def _zero_slice(self): ...

    @property
    def offset(self) -> int:
        return self._offset

    @offset.setter
    def offset(self, value: int):
        value = value or 0
        self._offset = int(value)

    @property
    def parent_start(self) -> int:
        """returns the start on the parent plus strand

        Returns
        -------
        offset + start, taking into account whether reversed. Result
        is positive.
        """
        if self.is_reversed:
            # self.stop becomes the start, self.stop will be negative
            assert self.stop < 0, "expected stop on reverse strand SeqView < 0"
            start = self.stop + self.seq_len + 1
        else:
            start = self.start

        return self.offset + start

    @property
    def is_reversed(self):
        return self.step < 0

    @property
    def parent_stop(self) -> int:
        """returns the stop on the parent plus strand

        Returns
        -------
        offset + stop, taking into account whether reversed. Result
        is positive.
        """
        if self.is_reversed:
            # self.start becomes the stop, self.start will be negative
            assert self.start < 0, "expected start on reverse strand SeqView < 0"
            stop = self.start + self.seq_len + 1
        else:
            stop = self.stop
        return self.offset + stop

    def absolute_position(self, rel_index: int, include_boundary: bool = False):
        """Converts an index relative to the current view to be with respect
        to the coordinates of the original "Python sequence".

        Parameters
        ----------
        rel_index
            relative position with respect to the current view

        Returns
        -------
        the absolute index with respect to the coordinates of the original
        sequence (including offset if present).
        """
        if not self:
            return 0

        if rel_index < 0:
            raise IndexError("only positive indexing supported!")

        # _get_index return the absolute position relative to the underlying sequence
        seq_index, _, _ = self._get_index(rel_index, include_boundary=include_boundary)

        offset = self.offset
        return (
            offset + self.seq_len + seq_index + 1
            if self.is_reversed
            else offset + seq_index
        )

    def relative_position(self, abs_index: int, stop: bool = False):
        """converts an index on the original "Python sequence" into an index
        on this "view"

        Notes
        -----
        The returned value DOES NOT reflect python indexing. Importantly,
        negative values represent positions that precede the current view.
        """
        if not self:
            return 0

        if abs_index < 0:
            raise IndexError("Index must be +ve and relative to the + strand")

        if self.is_reversed:
            offset = self.offset

            if (
                tmp := (self.seq_len - abs_index + offset + self.start + 1)
            ) % self.step == 0 or stop:
                rel_pos = tmp // abs(self.step)
            else:
                rel_pos = (tmp // abs(self.step)) + 1

        else:
            offset = self.offset + self.start

            if (tmp := abs_index - offset) % self.step == 0 or stop:
                rel_pos = tmp // self.step
            else:
                rel_pos = (tmp // self.step) + 1

        return rel_pos

    def __len__(self):
        return abs((self.start - self.stop) // self.step)

    def __getitem__(self, segment: typing.Union[int, slice]):
        kwargs = self._get_init_kwargs()

        if _is_int(segment):
            start, stop, step = self._get_index(segment)
            return self.__class__(
                start=start,
                stop=stop,
                step=step,
                offset=self.offset,
                seq_len=self.seq_len,
                **kwargs,
            )

        if segment.start is segment.stop is segment.step is None:
            return self.copy()

        if len(self) == 0:
            return self

        if segment.start is not None and segment.start == segment.stop:
            return self._zero_slice

        slice_step = 1 if segment.step is None else segment.step

        if slice_step > 0:
            return self._get_slice(segment, slice_step, **kwargs)
        elif slice_step < 0:
            return self._get_reverse_slice(segment, slice_step, **kwargs)
        else:
            raise ValueError(
                f"{self.__class__.__name__} cannot be sliced with a step of 0"
            )

    def _get_index(self, val: int, include_boundary: bool = False):
        if len(self) == 0:
            raise IndexError(val)

        if val > 0 and include_boundary and val > len(self):
            raise IndexError(val)
        elif val > 0 and not include_boundary and val >= len(self):
            raise IndexError(val)
        elif val < 0 and include_boundary and abs(val) > (len(self) + 1):
            raise IndexError(val)
        elif val < 0 and not include_boundary and abs(val) > len(self):
            raise IndexError(val)

        if self.step > 0:
            if val >= 0:
                val = self.start + val * self.step
            else:
                val = self.start + len(self) * self.step + val * abs(self.step)

            return val, val + 1, 1

        elif self.step < 0:
            if val >= 0:
                val = self.start + val * self.step
            else:
                val = self.start + len(self) * self.step + val * self.step

            return val, val - 1, -1

    def _get_slice(self, segment: slice, step: int, **kwargs):
        slice_start = segment.start if segment.start is not None else 0
        slice_stop = segment.stop if segment.stop is not None else len(self)

        if self.step > 0:
            return self._get_forward_slice_from_forward_seqview_(
                slice_start, slice_stop, step, **kwargs
            )

        elif self.step < 0:
            return self._get_forward_slice_from_reverse_seqview_(
                slice_start, slice_stop, step, **kwargs
            )

    def _get_forward_slice_from_forward_seqview_(
        self, slice_start: int, slice_stop: int, step: int, **kwargs
    ):
        start = (
            self.start + slice_start * self.step
            if slice_start >= 0
            else max(
                self.start + len(self) * self.step + slice_start * self.step,
                self.start,
            )
        )
        if slice_stop > self.stop:
            stop = self.stop
        elif slice_stop >= 0:
            stop = self.start + slice_stop * self.step
        else:
            # "true stop" adjust for if abs(stop-start) % step != 0
            # "true stop" = self.start + len(self) * self.step
            stop = self.start + len(self) * self.step + slice_stop * self.step

        # if -ve, it's an invalid slice
        if start < 0 or stop < 0:
            return self._zero_slice

        # checking for zero-length slice
        if stop < start:
            return self._zero_slice
        if start > self.seq_len:
            return self._zero_slice

        return self.__class__(
            start=start,
            stop=min(self.stop, stop),
            step=self.step * step,
            offset=self.offset,
            seq_len=self.seq_len,
            **kwargs,
        )

    def _get_forward_slice_from_reverse_seqview_(
        self, slice_start: int, slice_stop: int, step: int, **kwargs
    ):
        if slice_start >= 0:
            start = self.start + slice_start * self.step
        elif abs(slice_start) > len(self):
            start = self.start
        else:
            start = self.start + len(self) * self.step + slice_start * self.step

        if slice_stop >= 0:
            stop = self.start + slice_stop * self.step
        else:  # slice_stop < 0
            stop = self.start + len(self) * self.step + slice_stop * self.step

        # if +ve, it's an invalid slice
        if start >= 0 or stop >= 0:
            return self._zero_slice

        return self.__class__(
            start=start,
            stop=max(self.stop, stop),
            step=self.step * step,
            offset=self.offset,
            seq_len=self.seq_len,
            **kwargs,
        )

    def _get_reverse_slice(self, segment: slice, step: int, **kwargs):
        slice_start = segment.start if segment.start is not None else -1
        slice_stop = segment.stop if segment.stop is not None else -len(self) - 1

        if self.step < 0:
            return self._get_reverse_slice_from_reverse_seqview_(
                slice_start, slice_stop, step, **kwargs
            )
        elif self.step > 0:
            return self._get_reverse_slice_from_forward_seqview_(
                slice_start, slice_stop, step, **kwargs
            )

    def _get_reverse_slice_from_forward_seqview_(
        self, slice_start: int, slice_stop: int, step: int, **kwargs
    ):
        # "true stop" adjust for if abs(stop-start) % step != 0
        # max possible start is "true stop" - step, because stop is not inclusive
        # "true stop" - step is converted to -ve index via subtracting len(self)
        if slice_start >= len(self):
            start = (self.start + len(self) * self.step - self.step) - self.seq_len
        elif slice_start >= 0:
            start = (self.start + slice_start * self.step) - self.seq_len
        else:
            start = (
                self.start
                + len(self) * self.step
                + slice_start * self.step
                - self.seq_len
            )

        if slice_stop >= self.seq_len:
            return self._zero_slice

        if slice_stop >= 0:
            stop = self.start + (slice_stop * self.step) - self.seq_len
        else:
            stop = (
                self.start
                + (len(self) * self.step)
                + (slice_stop * self.step)
                - self.seq_len
            )

        if start >= 0 or stop >= 0:
            return self._zero_slice

        return self.__class__(
            start=start,
            stop=max(stop, self.start - self.seq_len - 1),
            step=self.step * step,
            offset=self.offset,
            seq_len=self.seq_len,
            **kwargs,
        )

    def _get_reverse_slice_from_reverse_seqview_(
        self, slice_start: int, slice_stop: int, step: int, **kwargs
    ):
        # max start is "true stop" + abs(step), because stop is not inclusive
        # "true stop" adjust for if abs(stop-start) % step != 0
        if slice_start >= len(self):
            start = self.seq_len + self.start + len(self) * self.step + abs(self.step)
        elif slice_start >= 0:
            start = self.seq_len + (self.start + slice_start * self.step)
        else:
            start = self.seq_len + (
                self.start + len(self) * self.step + slice_start * self.step
            )

        if slice_stop >= 0:
            stop = self.seq_len + (self.start + slice_stop * self.step)
            if stop <= self.seq_len + self.stop:
                return self._zero_slice
        else:
            stop = self.seq_len + (
                self.start + len(self) * self.step + slice_stop * self.step
            )
            if stop > self.seq_len + self.start:
                stop = self.seq_len + self.start + 1

        # if -ve, it's an invalid slice becomes zero
        # checking for zero-length slice
        if stop < start or start > self.seq_len or min(start, stop) < 0:
            return self._zero_slice

        return self.__class__(
            start=start,
            stop=stop,
            step=self.step * step,
            offset=self.offset,
            seq_len=self.seq_len,
            **kwargs,
        )


class SeqViewABC(ABC):
    # refactor: docstring

    __slots__ = ()

    @property
    def seqid(self) -> OptStr: ...

    @property
    @abstractmethod
    def str_value(self) -> str: ...

    @property
    @abstractmethod
    def array_value(self) -> array: ...

    @property
    @abstractmethod
    def bytes_value(self) -> bytes: ...

    @abstractmethod
    def copy(self, sliced: bool = False): ...

    def __str__(self) -> str:
        return self.str_value

    def __array__(self, dtype=None):
        arr = self.array_value
        if dtype is not None:
            arr = arr.astype(dtype)
        return arr

    def __bytes__(self):
        return self.bytes_value


class SeqView(SeqViewABC, SliceRecordABC):
    # refactor: docstring
    __slots__ = (
        "seq",
        "alphabet",
        "start",
        "stop",
        "step",
        "_offset",
        "_seqid",
        "_seq_len",
    )

    def __init__(
        self,
        *,
        seq: str,
        alphabet: new_alphabet.AlphabetABC,
        start: OptInt = None,
        stop: OptInt = None,
        step: OptInt = None,
        offset: int = 0,
        seqid: OptStr = None,
        seq_len: OptInt = None,
    ):
        if step == 0:
            raise ValueError("step cannot be 0")
        step = 1 if step is None else step
        self.alphabet = alphabet
        func = _input_vals_pos_step if step > 0 else _input_vals_neg_step
        start, stop, step = func(len(seq), start, stop, step)
        self.seq = seq
        self.start = start
        self.stop = stop
        self.step = step
        self._offset = offset
        self._seqid = seqid

        if seq_len is not None and seq_len != len(seq):
            raise AssertionError(f"{seq_len} != {len(self.seq)})")
        self._seq_len = seq_len or len(self.seq)

    @property
    def _zero_slice(self):
        return self.__class__(seq="", alphabet=self.alphabet)

    @property
    def seqid(self) -> str:
        return self._seqid

    @property
    def seq_len(self) -> int:
        return self._seq_len

    def _get_init_kwargs(self):
        return {"seq": self.seq, "seqid": self.seqid, "alphabet": self.alphabet}

    @property
    def str_value(self):
        return self.seq[self.start : self.stop : self.step]

    @property
    def array_value(self):
        return self.alphabet.to_indices(self.str_value)

    @property
    def bytes_value(self):
        return self.str_value.encode("utf-8")

    def __repr__(self) -> str:
        seq = f"{self.seq[:10]}...{self.seq[-5:]}" if self.seq_len > 15 else self.seq
        return (
            f"{self.__class__.__name__}(seq={seq!r}, start={self.start}, "
            f"stop={self.stop}, step={self.step}, offset={self.offset}, "
            f"seqid={self.seqid!r}, seq_len={self.seq_len})"
        )

    def to_rich_dict(self):
        # get the current state
        data = {"type": get_object_provenance(self), "version": __version__}
        data["init_args"] = self._get_init_kwargs()
        # since we will truncate the seq, we don't need start, stop,
        # step is sufficient
        data["init_args"]["step"] = self.step
        if self.is_reversed:
            adj = self.seq_len + 1
            start, stop = self.stop + adj, self.start + adj
        else:
            start, stop = self.start, self.stop

        data["init_args"]["seq"] = self.seq[start:stop]
        data["init_args"]["offset"] = int(self.parent_start)
        return data

    @classmethod
    def from_rich_dict(cls, data: dict):
        init_args = data.pop("init_args")
        if "offset" in data:
            init_args["offset"] = data.pop("offset")
        return cls(**init_args)

    def copy(self, sliced: bool = False):
        """returns copy

        Parameters
        ----------
        sliced
            if True, the underlying sequence is truncated and the start/stop
            adjusted
        """
        if not sliced:
            return self.__class__(
                seq=self.seq,
                seqid=self.seqid,
                alphabet=self.alphabet,
                start=self.start,
                stop=self.stop,
                step=self.step,
                offset=self.offset,
                seq_len=self.seq_len,
            )
        return self.from_rich_dict(self.to_rich_dict())


class DnaSequence(Sequence, NucleicAcidSequenceMixin):
    """Holds the standard DNA sequence."""

    # constructed by DNA moltype

    def _seq_filter(self, seq):
        """Converts U to T."""
        return seq.replace("u", "t").replace("U", "T")


class RnaSequence(Sequence, NucleicAcidSequenceMixin):
    """Holds the standard RNA sequence."""

    def _seq_filter(self, seq):
        """Converts T to U."""
        return seq.replace("t", "u").replace("T", "U")


@singledispatch
def _coerce_to_seqview(data, seqid, alphabet) -> SeqViewABC:
    from cogent3.core.alignment import Aligned

    if isinstance(data, Aligned):
        return _coerce_to_seqview(str(data), seqid, alphabet)
    raise NotImplementedError(f"{type(data)}")


@_coerce_to_seqview.register
def _(data: SeqViewABC, seqid, alphabet) -> SeqViewABC:
    return data


@_coerce_to_seqview.register
def _(data: Sequence, seqid, alphabet) -> SeqViewABC:
    return _coerce_to_seqview(data._seq, seqid, alphabet)


@_coerce_to_seqview.register
def _(data: str, seqid, alphabet) -> SeqViewABC:
    return SeqView(seq=data, seqid=seqid, alphabet=alphabet)


@_coerce_to_seqview.register
def _(data: bytes, seqid, alphabet) -> SeqViewABC:
    data = data.decode("utf8")
    return SeqView(seq=data, seqid=seqid, alphabet=alphabet)


@_coerce_to_seqview.register
def _(data: tuple, seqid, alphabet) -> SeqViewABC:
    return _coerce_to_seqview("".join(data), seqid, alphabet)


@_coerce_to_seqview.register
def _(data: list, seqid, alphabet) -> SeqViewABC:
    return _coerce_to_seqview("".join(data), seqid, alphabet)