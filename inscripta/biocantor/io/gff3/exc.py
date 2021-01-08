from inscripta.biocantor.io.exc import InvalidInputError
from inscripta.biocantor.exc import BioCantorException


class GFF3FastaException(InvalidInputError):
    """
    Raised when sequence export fails, or there is an attempt to export sequence without sequence information.
    """

    pass


class GFF3ExportException(BioCantorException):
    """
    Raised for any generic error when exporting a GFF3.
    """

    pass


class ReservedKeyWarning(UserWarning):
    """
    Used when a GFF3 writing event has a qualifier with a reserved GFF3 key.
    """

    pass


class Gff3ParserError(InvalidInputError):
    """
    Raised when there is a parsing exception.
    """

    pass


class EmptyGff3Exception(InvalidInputError):
    """
    Raised when parsing produces an empty GFF3.
    """

    pass
