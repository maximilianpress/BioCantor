"""
Collection classes. The data model is structured into two general categories,
transcripts and features. Each of those are wrapped into genes and feature collections,
respectively. These are then wrapped up into one :class:`AnnotationIntervalCollection`.

:class:`AnnotationIntervalCollections` are the topmost class and hold all possible annotations
for a given interval, as well as the place to find their sequence information.

It is useful to think of transcripts/genes as *transcriptional units*, which mean these data structures
model *transcribed sequence*. In contrast, features are *non-transcribed*, and are meant to model things
such as promoters or transcription factor binding sites.

Each object is capable of exporting itself to BED and GFF3.
"""
import itertools
from abc import ABC, abstractmethod
from functools import reduce
from typing import List, Iterable, Any, Dict, Set, Hashable, Optional, Union
from uuid import UUID

from inscripta.biocantor.exc import (
    ValidationException,
    NoncodingTranscriptError,
    InvalidAnnotationError,
    InvalidQueryError,
)
from inscripta.biocantor.gene.biotype import Biotype
from inscripta.biocantor.gene.cds import CDSInterval, CDSPhase
from inscripta.biocantor.gene.feature import FeatureInterval, AbstractInterval, QualifierValue
from inscripta.biocantor.gene.transcript import TranscriptInterval
from inscripta.biocantor.io.gff3.constants import GFF_SOURCE, NULL_COLUMN, BioCantorQualifiers, BioCantorFeatureTypes
from inscripta.biocantor.io.gff3.rows import GFFRow, GFFAttributes
from inscripta.biocantor.io.gff3.exc import GFF3MissingSequenceNameError
from inscripta.biocantor.location import Location
from inscripta.biocantor.location.location_impl import SingleInterval, CompoundInterval, EmptyLocation
from inscripta.biocantor.location.strand import Strand
from inscripta.biocantor.parent.parent import Parent
from inscripta.biocantor.sequence import Sequence
from inscripta.biocantor.util.bins import bins
from inscripta.biocantor.util.hashing import digest_object
from inscripta.biocantor.util.object_validation import ObjectValidation


class AbstractFeatureIntervalCollection(AbstractInterval, ABC):
    """
    Abstract class for holding groups of feature intervals. The two implementations of this class
    model Genes or non-transcribed FeatureCollections.

    These are always on the same sequence, but can be on different strands.
    """

    @abstractmethod
    def children_guids(self) -> Set[UUID]:
        """Get all of the GUIDs for children.

        Returns: A set of UUIDs
        """

    def get_reference_sequence(self) -> Sequence:
        """Returns the *plus strand* sequence for this interval"""
        return self.location.extract_sequence()

    @staticmethod
    def _find_primary_feature(
        intervals: Union[List[TranscriptInterval], List[FeatureInterval]]
    ) -> Optional[Union[TranscriptInterval, FeatureInterval]]:
        """
        Used in object construction to find the primary feature. Shared between :class:`GeneInterval`
        and :class:`FeatureIntervalCollection`.
        """
        # see if we were given a primary feature
        primary_feature = None
        for i, interval in enumerate(intervals):
            if interval.is_primary_feature:
                if primary_feature:
                    raise ValidationException("Multiple primary features/transcripts found")
                primary_feature = intervals[i]
        # if no primary interval was given, then infer by longest CDS then longest interval
        # if this is a feature, then there is no CDS, so set that value to 0
        if primary_feature is None:
            interval_sizes = sorted(
                (
                    [interval.cds_size if hasattr(interval, "cds_size") else 0, len(interval), i]
                    for i, interval in enumerate(intervals)
                ),
                key=lambda x: (-x[0], -x[1]),
            )
            primary_feature = intervals[interval_sizes[0][2]]
        return primary_feature


class GeneInterval(AbstractFeatureIntervalCollection):
    """
    A GeneInterval is a collection of :class:`~biocantor.gene.transcript.TranscriptInterval` for a specific locus.

    This is a traditional gene model. By this, I mean that there is one continuous region that defines the gene.
    This region then contains 1 to N subregions that are transcripts. These transcripts may or may not be coding,
    and there is no requirement that all transcripts have the same type. Each transcript consists of one or more
    intervals, and can exist on either strand. There is no requirement that every transcript exist on the same strand.

    The ``Strand`` of this gene interval is always the *plus* strand.

    This cannot be empty; it must have at least one transcript.

    If a ``primary_transcript`` is not provided, then it is inferred by the hierarchy of longest CDS followed by
    longest isoform.
    """

    _identifiers = ["gene_id", "gene_symbol", "locus_tag"]

    def __init__(
        self,
        transcripts: List[TranscriptInterval],
        guid: Optional[UUID] = None,
        gene_id: Optional[str] = None,
        gene_symbol: Optional[str] = None,
        gene_type: Optional[Biotype] = None,
        locus_tag: Optional[str] = None,
        qualifiers: Optional[Dict[Hashable, List[QualifierValue]]] = None,
        sequence_name: Optional[str] = None,
        sequence_guid: Optional[UUID] = None,
        parent: Optional[Parent] = None,
    ):
        self.transcripts = transcripts
        self.gene_id = gene_id
        self.gene_symbol = gene_symbol
        self.gene_type = gene_type
        self.locus_tag = locus_tag
        self.sequence_name = sequence_name
        self.sequence_guid = sequence_guid
        # qualifiers come in as a List, convert to Set
        self._import_qualifiers_from_list(qualifiers)

        if not self.transcripts:
            raise InvalidAnnotationError("GeneInterval must have transcripts")

        start = min(tx.start for tx in self.transcripts)
        end = max(tx.end for tx in self.transcripts)
        self.location = SingleInterval(start, end, Strand.PLUS, parent=parent)
        self.bin = bins(start, end, fmt="bed")
        self.primary_transcript = AbstractFeatureIntervalCollection._find_primary_feature(self.transcripts)

        if guid is None:
            self.guid = digest_object(
                self.location,
                self.gene_id,
                self.gene_symbol,
                self.gene_type,
                self.locus_tag,
                self.sequence_name,
                self.qualifiers,
                self.children_guids,
            )
        else:
            self.guid = guid

        if self.location.parent:
            ObjectValidation.require_location_has_parent_with_sequence(self.location)

    def __repr__(self):
        return f"{self.__class__.__name__}({','.join(str(tx) for tx in self.transcripts)})"

    def __iter__(self):
        yield from self.transcripts

    @property
    def is_coding(self) -> bool:
        """One or more coding isoforms?"""
        return any(tx.is_coding for tx in self.transcripts)

    @property
    def children_guids(self):
        return {x.guid for x in self.transcripts}

    def to_dict(self) -> Dict[Any, str]:
        """Convert to a dict usable by :class:`~biocantor.io.models.GeneIntervalModel`."""
        return dict(
            transcripts=[tx.to_dict() for tx in self.transcripts],
            gene_id=self.gene_id,
            gene_symbol=self.gene_symbol,
            gene_type=self.gene_type.name if self.gene_type else None,
            locus_tag=self.locus_tag,
            qualifiers=self._export_qualifiers_to_list(),
            sequence_name=self.sequence_name,
            sequence_guid=self.sequence_guid,
            gene_guid=self.guid,
        )

    def get_primary_transcript(self) -> Union[TranscriptInterval, None]:
        """Get the primary transcript, if it exists."""

        return self.primary_transcript

    def get_primary_cds(self) -> Union[CDSInterval, None]:
        """Get the CDS of the primary transcript, if it exists."""
        if self.get_primary_transcript() is not None:
            return self.primary_transcript.cds

    def get_primary_transcript_sequence(self) -> Union[Sequence, None]:
        """Get the sequence of the primary transcript, if it exists."""
        if self.get_primary_transcript() is not None:
            return self.primary_transcript.get_spliced_sequence()

    def get_primary_cds_sequence(self) -> Union[Sequence, None]:
        """Get the sequence of the primary transcript, if it exists."""
        if self.get_primary_transcript() is not None:
            return self.primary_transcript.cds.extract_sequence()

    def get_primary_protein(self) -> Union[Sequence, None]:
        """Get the protein sequence of the primary transcript, if it exists."""
        if self.get_primary_cds() is not None:
            return self.primary_transcript.cds.translate()

    def _produce_merged_feature(self, intervals: List[Location]) -> FeatureInterval:
        """Wrapper function used by both :func:`GeneInterval.get_merged_transcript`
        and :func:`GeneInterval.get_merged_cds`.
        """
        merged = reduce(lambda x, y: x.union(y), intervals)
        interval_starts = [x.start for x in merged.blocks]
        interval_ends = [x.end for x in merged.blocks]
        loc = CompoundInterval(interval_starts, interval_ends, self.location.strand, parent=self.location.parent)
        return FeatureInterval(
            loc,
            qualifiers=self._export_qualifiers_to_list(),
            sequence_guid=self.sequence_guid,
            sequence_name=self.sequence_name,
            feature_types=[self.gene_type.name],
            feature_name=self.gene_symbol,
            feature_id=self.gene_id,
            locus_tag=self.locus_tag,
            guid=self.guid,
        )

    def get_merged_transcript(self) -> FeatureInterval:
        """Generate a single :class:`~biocantor.gene.feature.FeatureInterval` that merges all exons together.

        This inherently has no translation and so is returned as a generic feature, not a transcript.
        """
        intervals = []
        for tx in self.transcripts:
            for i in tx.location.blocks:
                intervals.append(i)
        return self._produce_merged_feature(intervals)

    def get_merged_cds(self) -> FeatureInterval:
        """Generate a single :class:`~biocantor.gene.feature.FeatureInterval` that merges all CDS intervals."""
        intervals = []
        for tx in self.transcripts:
            if tx.is_coding:
                for i in tx.cds.location.blocks:
                    intervals.append(i)
        if not intervals:
            raise NoncodingTranscriptError("No CDS transcripts found on this gene")
        return self._produce_merged_feature(intervals)

    def export_qualifiers(self) -> Dict[Hashable, Set[str]]:
        """Exports qualifiers for GFF3/GenBank export"""
        qualifiers = self.qualifiers.copy()
        for key, val in [
            [BioCantorQualifiers.GENE_ID.value, self.gene_id],
            [BioCantorQualifiers.GENE_NAME.value, self.gene_symbol],
            [BioCantorQualifiers.GENE_TYPE.value, self.gene_type.name if self.gene_type else None],
            [BioCantorQualifiers.LOCUS_TAG.value, self.locus_tag],
        ]:
            if not val:
                continue
            if key not in qualifiers:
                qualifiers[key] = set()
            qualifiers[key].add(val)
        return qualifiers

    def to_gff(self) -> Iterable[GFFRow]:
        """Produces iterable of :class:`~biocantor.io.gff3.rows.GFFRow` for this gene and its children.

        Yields:
            :class:`~biocantor.io.gff3.rows.GFFRow`
        """
        if not self.sequence_name:
            raise GFF3MissingSequenceNameError("Must have sequence names to export to GFF3.")

        qualifiers = self.export_qualifiers()

        gene_guid = str(self.guid)

        attributes = GFFAttributes(id=gene_guid, qualifiers=qualifiers, name=self.gene_symbol, parent=None)
        row = GFFRow(
            self.sequence_name,
            GFF_SOURCE,
            BioCantorFeatureTypes.GENE,
            self.start + 1,
            self.end,
            NULL_COLUMN,
            self.location.strand,
            CDSPhase.NONE,
            attributes,
        )
        yield row
        for tx in self.transcripts:
            yield from tx.to_gff(gene_guid, qualifiers)


class FeatureIntervalCollection(AbstractFeatureIntervalCollection):
    """A FeatureIntervalCollection is arbitrary container of intervals.

    This can be thought of to be analogous to a :class:`GeneInterval`, but for non-transcribed
    features that are grouped in some fashion. An example is transcription factor
    binding sites for a specific transcription factor.

    The :class:`~biocantor.location.strand.Strand` of this feature interval collection is always the *plus* strand.

    This cannot be empty; it must have at least one feature interval.
    """

    _identifiers = ["feature_collection_id", "feature_collection_name", "locus_tag"]

    def __init__(
        self,
        feature_intervals: List[FeatureInterval],
        feature_collection_name: Optional[str] = None,
        feature_collection_id: Optional[str] = None,
        locus_tag: Optional[str] = None,
        sequence_name: Optional[str] = None,
        sequence_guid: Optional[UUID] = None,
        guid: Optional[UUID] = None,
        qualifiers: Optional[Dict[Hashable, List[QualifierValue]]] = None,
        parent: Optional[Parent] = None,
    ):
        self.feature_intervals = feature_intervals
        self.feature_collection_name = feature_collection_name
        self.feature_collection_id = feature_collection_id
        self.locus_tag = locus_tag
        self.sequence_name = sequence_name
        self.sequence_guid = sequence_guid
        # qualifiers come in as a List, convert to Set
        self._import_qualifiers_from_list(qualifiers)

        feature_types = [x.feature_types for x in feature_intervals if x.feature_types]
        if feature_types:
            self.feature_types = set.union(*feature_types)
        else:
            self.feature_types = None

        if not self.feature_intervals:
            raise InvalidAnnotationError("FeatureCollection must have features")

        start = min(f.start for f in self.feature_intervals)
        end = max(f.end for f in self.feature_intervals)
        self.location = SingleInterval(start, end, Strand.PLUS, parent=parent)
        self.bin = bins(start, end, fmt="bed")

        self.primary_feature = AbstractFeatureIntervalCollection._find_primary_feature(self.feature_intervals)

        if guid is None:
            self.guid = digest_object(
                self.location,
                self.feature_collection_name,
                self.feature_collection_id,
                self.feature_types,
                self.locus_tag,
                self.sequence_name,
                self.qualifiers,
                self.children_guids,
            )
        else:
            self.guid = guid

        if self.location.parent:
            ObjectValidation.require_location_has_parent_with_sequence(self.location)

    def __repr__(self):
        return f"{self.__class__.__name__}({','.join(str(f) for f in self.feature_intervals)})"

    def __iter__(self) -> Iterable[FeatureInterval]:
        """Iterate over all intervals in this collection."""
        yield from self.feature_intervals

    @property
    def is_coding(self) -> bool:
        """Never coding."""
        return False

    @property
    def children_guids(self) -> Set[UUID]:
        return {x.guid for x in self.feature_intervals}

    def get_primary_feature(self) -> Union[FeatureInterval, None]:
        """Get the primary transcript, if it exists."""

        return self.primary_feature

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a dict usable by :class:`~biocantor.io.models.FeatureIntervalCollectionModel`."""
        return dict(
            feature_intervals=[feat.to_dict() for feat in self.feature_intervals],
            feature_collection_name=self.feature_collection_name,
            feature_collection_id=self.feature_collection_id,
            locus_tag=self.locus_tag,
            qualifiers=self._export_qualifiers_to_list(),
            sequence_name=self.sequence_name,
            sequence_guid=self.sequence_guid,
            feature_collection_guid=self.guid,
        )

    def export_qualifiers(self) -> Dict[Hashable, Set[str]]:
        """Exports qualifiers for GFF3/GenBank export"""
        qualifiers = self.qualifiers.copy()
        for key, val in [
            [BioCantorQualifiers.FEATURE_COLLECTION_ID.value, self.feature_collection_id],
            [BioCantorQualifiers.FEATURE_COLLECTION_NAME.value, self.feature_collection_name],
            [BioCantorQualifiers.LOCUS_TAG.value, self.locus_tag],
        ]:
            if not val:
                continue
            if key not in qualifiers:
                qualifiers[key] = set()
            qualifiers[key].add(val)
        if self.feature_types:
            qualifiers[BioCantorQualifiers.FEATURE_TYPE.value] = self.feature_types
        return qualifiers

    def to_gff(self) -> Iterable[GFFRow]:
        """Produces iterable of :class:`~biocantor.io.gff3.rows.GFFRow` for this feature and its children.

        Yields:
            :class:`~biocantor.io.gff3.rows.GFFRow`
        """
        if not self.sequence_name:
            raise GFF3MissingSequenceNameError("Must have sequence names to export to GFF3.")

        qualifiers = self.export_qualifiers()

        feat_group_id = str(self.guid)

        attributes = GFFAttributes(
            id=feat_group_id, qualifiers=qualifiers, name=self.feature_collection_name, parent=None
        )

        row = GFFRow(
            self.sequence_name,
            GFF_SOURCE,
            BioCantorFeatureTypes.FEATURE_COLLECTION,
            self.start + 1,
            self.end,
            NULL_COLUMN,
            self.location.strand,
            CDSPhase.NONE,
            attributes,
        )
        yield row

        for feature in self.feature_intervals:
            yield from feature.to_gff(feat_group_id, qualifiers)


class AnnotationCollection(AbstractFeatureIntervalCollection):
    """An AnnotationCollection is a container to contain :class:`GeneInterval`s and
    :class:`FeatureIntervalCollection`s.

    Encapsulates all possible annotations for a given interval on a specific source.

    If no start/end points are provided, the interval for this collection is the min/max of the data it contains. The
    interval for an AnnotationCollection is always on the plus strand.

    An AnnotationCollection can be empty.
    """

    _identifiers = ["name"]

    def __init__(
        self,
        feature_collections: Optional[List[FeatureIntervalCollection]] = None,
        genes: Optional[List[GeneInterval]] = None,
        name: Optional[str] = None,
        sequence_name: Optional[str] = None,
        sequence_guid: Optional[UUID] = None,
        qualifiers: Optional[Dict[Hashable, QualifierValue]] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        completely_within: Optional[bool] = None,
        parent: Optional[Parent] = None,
    ):

        self.feature_collections = feature_collections if feature_collections else []
        self.genes = genes if genes else []
        self.sequence_name = sequence_name
        self.sequence_guid = sequence_guid
        self.name = name
        # qualifiers come in as a List, convert to Set
        self._import_qualifiers_from_list(qualifiers)

        # we store the sequence explicitly, because this is how we can retain sequence information
        # for empty collections
        if parent and parent.sequence:
            self.sequence = parent.sequence
        else:
            self.sequence = None

        if start is None and end is None:
            # if we have nothing, we cannot infer a range
            if not self.is_empty:
                if start is None:
                    start = min(f.start for f in self.iter_children())
                if end is None:
                    end = max(f.end for f in self.iter_children())

        if start is None and end is None:
            # if we still have nothing, we are empty
            self.location = EmptyLocation()
        else:
            assert start is not None and end is not None
            self.location = SingleInterval(start, end, Strand.PLUS, parent=parent)
            self.bin = bins(self.start, self.end, fmt="bed")
        self.completely_within = completely_within

        self._guid_map = {}
        self._guid_cached = False

        self.guid = digest_object(
            self.location, self.name, self.sequence_name, self.qualifiers, self.completely_within, self.children_guids
        )

        if self.location.parent:
            ObjectValidation.require_location_has_parent_with_sequence(self.location)

    def __repr__(self):
        return f"{self.__class__.__name__}({','.join(str(f) for f in self.iter_children())})"

    def __iter__(self) -> Iterable[Union[GeneInterval, FeatureIntervalCollection]]:
        """Iterate over all intervals in this collection."""
        yield from self.iter_children()

    def __len__(self):
        return len(self.feature_collections) + len(self.genes)

    @property
    def is_empty(self) -> bool:
        """Is this an empty collection?"""
        return len(self) == 0

    @property
    def children_guids(self) -> set:
        return {x.guid for x in self.iter_children()}

    @property
    def hierarchical_children_guids(self) -> Dict[UUID, Set[UUID]]:
        """Returns children GUIDs in their hierarchical structure."""
        retval = {}
        for child in self.iter_children():
            retval[child.guid] = child.children_guids
        return retval

    def iter_children(self) -> Iterable[Union[GeneInterval, FeatureIntervalCollection]]:
        """Iterate over all intervals in this collection, in sorted order."""
        chain_iter = itertools.chain(self.genes, self.feature_collections)
        sort_iter = sorted(chain_iter, key=lambda x: x.start)
        yield from sort_iter

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a dict usable by :class:`~biocantor.io.models.AnnotationCollectionModel`."""
        return dict(
            genes=[gene.to_dict() for gene in self.genes],
            feature_collections=[feature.to_dict() for feature in self.feature_collections],
            name=self.name,
            qualifiers=self._export_qualifiers_to_list(),
            sequence_name=self.sequence_name,
            sequence_guid=self.sequence_guid,
            start=self.start,
            end=self.end,
            completely_within=self.completely_within,
        )

    def query_by_position(
        self,
        start: Optional[int] = None,
        end: Optional[int] = None,
        coding_only: Optional[bool] = False,
        completely_within: Optional[bool] = True,
    ) -> "AnnotationCollection":
        """Filter this annotation collection object based on positions, sequence, and boolean flags.

        Args:
            start: Start position. If not set, will be 0.
            end: End position. If not set, will be unbounded.
            coding_only: Filter for coding genes only?
            completely_within: Strict boundaries? If False, features that partially overlap
                will be included in the output. Bins optimization cannot be used.

        Returns:
           :class:`AnnotationCollection` that may be empty.
        """
        # bins are only valid if we have start, end and completely_within
        if completely_within and start and end:
            my_bins = bins(start, end, fmt="bed", one=False)
        else:
            my_bins = None

        # after bins were decided, we can now force start/end to min/max values
        # for exact checking
        start = 0 if start is None else start
        end = self.end if end is None else end
        if start < 0:
            raise InvalidQueryError("Start must be positive")
        elif start > end:
            raise InvalidQueryError("Start must be less than or equal to end")

        genes_to_keep = []
        features_to_keep = []
        for gene_or_feature in self.iter_children():
            if coding_only and not gene_or_feature.is_coding:
                continue
            # my_bins only exists if completely_within, start and end
            if my_bins and gene_or_feature.bin not in my_bins:
                continue
            if completely_within and gene_or_feature.start >= start and gene_or_feature.end <= end:
                if isinstance(gene_or_feature, FeatureIntervalCollection):
                    features_to_keep.append(gene_or_feature)
                else:
                    genes_to_keep.append(gene_or_feature)
            elif not completely_within and gene_or_feature.start < end and gene_or_feature.end > start:
                if isinstance(gene_or_feature, FeatureIntervalCollection):
                    features_to_keep.append(gene_or_feature)
                else:
                    genes_to_keep.append(gene_or_feature)

        return AnnotationCollection(
            feature_collections=features_to_keep,
            genes=genes_to_keep,
            name=self.name,
            sequence_name=self.sequence_name,
            sequence_guid=self.sequence_guid,
            qualifiers=self._export_qualifiers_to_list(),
            start=start,
            end=end,
            completely_within=completely_within,
            parent=self.location.parent,
        )

    def _build_guid_cache(self):
        """
        If :meth:`AnnotationCollection.query_by_guids()` is called, then this function is called
        to populate the ``_guid_map`` member. Subsequent lookups are now ``O(1)``.
        """
        self._guid_map = {x.guid: x for x in self.iter_children()}
        self._guid_cached = True

    def query_by_guids(self, ids: List[UUID]) -> "AnnotationCollection":
        """Filter this annotation collection object by a list of unique IDs.

        This method is ``O(N)``, the first time it is used, and ``O(1)`` for subsequent uses because a lookup hash is
        built.

        Args:
            ids: List of GUIDs, or unique IDs.

        Returns:
           :class:`AnnotationCollection` that may be empty.
        """
        if self._guid_cached is False:
            self._build_guid_cache()

        ids = set(ids)
        genes_to_keep = []
        features_to_keep = []
        for i in ids:
            gene_or_feature = self._guid_map.get(i)
            if isinstance(gene_or_feature, FeatureIntervalCollection):
                features_to_keep.append(gene_or_feature)
            elif isinstance(gene_or_feature, GeneInterval):
                genes_to_keep.append(gene_or_feature)
            # otherwise this is None, which means we do not have a match.

        return AnnotationCollection(
            feature_collections=features_to_keep,
            genes=genes_to_keep,
            name=self.name,
            sequence_name=self.sequence_name,
            sequence_guid=self.sequence_guid,
            qualifiers=self._export_qualifiers_to_list(),
            start=self.start,
            end=self.end,
            parent=self.location.parent,
            completely_within=self.completely_within,
        )

    def query_by_feature_identifier(self, ids: List[str]) -> "AnnotationCollection":
        """Filter this annotation collection object by a list of identifiers.

        Identifiers are not necessarily unique; if your identifier matches more than one interval,
        all matching intervals will be returned. These ambiguous results will be adjacent in the resulting collection,
        but are not grouped or signified in any way.

        This method is ``O(n_ids * m_identifiers)``.

        Args:
            ids: List of identifiers.

        Returns:
           :class:`AnnotationCollection` that may be empty.
        """
        ids = set(ids)
        genes_to_keep = []
        features_to_keep = []
        for gene_or_feature in self.iter_children():
            if any(i in ids for i in gene_or_feature.identifiers):
                if isinstance(gene_or_feature, FeatureIntervalCollection):
                    features_to_keep.append(gene_or_feature)
                else:
                    genes_to_keep.append(gene_or_feature)

        return AnnotationCollection(
            feature_collections=features_to_keep,
            genes=genes_to_keep,
            name=self.name,
            sequence_name=self.sequence_name,
            sequence_guid=self.sequence_guid,
            qualifiers=self._export_qualifiers_to_list(),
            start=self.start,
            end=self.end,
            parent=self.location.parent,
            completely_within=self.completely_within,
        )

    def to_gff(self, ordered: Optional[bool] = False) -> Iterable[GFFRow]:
        """Produces iterable of :class:`~biocantor.io.gff3.rows.GFFRow` for this feature and its children.

        Yields:
            :class:`~biocantor.io.gff3.rows.GFFRow`
        """
        for item in self.iter_children():
            yield from item.to_gff()
