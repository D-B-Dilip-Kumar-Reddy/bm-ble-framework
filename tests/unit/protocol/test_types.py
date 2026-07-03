"""Unit tests for :mod:`bmd_ble.protocol.types`.

Covers the BMD data type enum and its width/struct-format lookup tables.
"""

import struct

from bmd_ble.protocol.types import (
    DATA_TYPE_BYTE_WIDTHS,
    DATA_TYPE_STRUCT_FORMATS,
    DataType,
)


class TestDataTypeValues:
    """Tests for the ``DataType`` enum values."""

    def test_data_type_values_match_protocol_spec(self):
        """Each DataType value matches the fixed BMD protocol data type table."""
        assert DataType.VOID == 0
        assert DataType.BOOL == 1
        assert DataType.INT8 == 2
        assert DataType.INT16 == 3
        assert DataType.INT32 == 4
        assert DataType.INT64 == 5
        assert DataType.STRING == 6
        assert DataType.FIXED16 == 7


class TestDataTypeByteWidths:
    """Tests for the ``DATA_TYPE_BYTE_WIDTHS`` lookup table."""

    def test_byte_widths_cover_every_data_type(self):
        """Every DataType member except STRING has a defined byte width."""
        for data_type in DataType:
            if data_type is DataType.STRING:
                continue
            assert data_type in DATA_TYPE_BYTE_WIDTHS

    def test_void_has_zero_width(self):
        """VOID carries no payload bytes."""
        assert DATA_TYPE_BYTE_WIDTHS[DataType.VOID] == 0

    def test_fixed_width_types_have_expected_byte_counts(self):
        """Fixed-width integer/bool/fixed16 types report the correct byte width."""
        assert DATA_TYPE_BYTE_WIDTHS[DataType.BOOL] == 1
        assert DATA_TYPE_BYTE_WIDTHS[DataType.INT8] == 1
        assert DATA_TYPE_BYTE_WIDTHS[DataType.INT16] == 2
        assert DATA_TYPE_BYTE_WIDTHS[DataType.INT32] == 4
        assert DATA_TYPE_BYTE_WIDTHS[DataType.INT64] == 8
        assert DATA_TYPE_BYTE_WIDTHS[DataType.FIXED16] == 2


class TestDataTypeStructFormats:
    """Tests for the ``DATA_TYPE_STRUCT_FORMATS`` lookup table."""

    def test_struct_formats_cover_fixed_width_types_only(self):
        """VOID and STRING are variable/absent and are excluded from the table."""
        assert DataType.VOID not in DATA_TYPE_STRUCT_FORMATS
        assert DataType.STRING not in DATA_TYPE_STRUCT_FORMATS

        for data_type in (
            DataType.BOOL,
            DataType.INT8,
            DataType.INT16,
            DataType.INT32,
            DataType.INT64,
            DataType.FIXED16,
        ):
            assert data_type in DATA_TYPE_STRUCT_FORMATS

    def test_struct_format_byte_size_matches_declared_width(self):
        """Each struct format code packs to the byte width declared in the width table."""
        for data_type, fmt in DATA_TYPE_STRUCT_FORMATS.items():
            assert struct.calcsize(f"<{fmt}") == DATA_TYPE_BYTE_WIDTHS[data_type]
