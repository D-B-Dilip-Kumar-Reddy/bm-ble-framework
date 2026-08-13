"""Unit tests for :mod:`bmd_camera.ble.protocol.types`.

Covers the BMD data type enum and its width/struct-format lookup tables.
The numeric coding is pinned to the official *Blackmagic Camera Control
Developer Information* table so a regression back to the repo's earlier
assumed enum (0=void, 1=bool, 2=int8, ..., 7=fixed16) fails loudly.
"""

import struct

from bmd_camera.ble.protocol.types import (
    DATA_TYPE_BYTE_WIDTHS,
    DATA_TYPE_STRUCT_FORMATS,
    DataType,
)


class TestDataTypeValues:
    """Tests for the ``DataType`` enum values."""

    def test_data_type_values_match_official_spec_coding(self):
        """Each DataType value matches the official BMD spec data type table."""
        assert DataType.VOID == 0
        assert DataType.INT8 == 1
        assert DataType.INT16 == 2
        assert DataType.INT32 == 3
        assert DataType.INT64 == 4
        assert DataType.STRING == 5
        assert DataType.FIXED16 == 128

    def test_bool_is_an_alias_of_void(self):
        """Spec code 0 is "void/boolean" — BOOL and VOID share the wire code."""
        assert DataType.BOOL is DataType.VOID
        assert DataType["BOOL"] is DataType.VOID
        assert DataType.BOOL == 0

    def test_sniffer_verified_recording_data_type_byte_is_int8(self):
        """The only sniffer-verified data-type byte (0x01, POCKET_6K_G2 v7.9
        recording command/echo) decodes to INT8 under the official coding."""
        assert DataType(0x01) is DataType.INT8

    def test_int16_array_is_the_candidate_recording_format_byte(self):
        """0x82 (INT16_ARRAY) is a CANDIDATE wire value from the POCKET_6K_G2
        v7.9 recording-format packet — not in the official spec coding."""
        assert DataType.INT16_ARRAY == 0x82
        assert DATA_TYPE_BYTE_WIDTHS[DataType.INT16_ARRAY] == 2
        assert struct.calcsize(DATA_TYPE_STRUCT_FORMATS[DataType.INT16_ARRAY]) == 2


class TestDataTypeByteWidths:
    """Tests for the ``DATA_TYPE_BYTE_WIDTHS`` lookup table."""

    def test_byte_widths_cover_every_data_type(self):
        """Every canonical DataType member except STRING has a defined byte width."""
        for data_type in DataType:
            if data_type is DataType.STRING:
                continue
            assert data_type in DATA_TYPE_BYTE_WIDTHS

    def test_void_has_zero_width(self):
        """VOID (the trigger reading of code 0) carries no payload bytes."""
        assert DATA_TYPE_BYTE_WIDTHS[DataType.VOID] == 0

    def test_fixed_width_types_have_expected_byte_counts(self):
        """Fixed-width integer/fixed16 types report the correct byte width."""
        assert DATA_TYPE_BYTE_WIDTHS[DataType.INT8] == 1
        assert DATA_TYPE_BYTE_WIDTHS[DataType.INT16] == 2
        assert DATA_TYPE_BYTE_WIDTHS[DataType.INT32] == 4
        assert DATA_TYPE_BYTE_WIDTHS[DataType.INT64] == 8
        assert DATA_TYPE_BYTE_WIDTHS[DataType.FIXED16] == 2


class TestDataTypeStructFormats:
    """Tests for the ``DATA_TYPE_STRUCT_FORMATS`` lookup table."""

    def test_struct_formats_cover_fixed_width_types_only(self):
        """Code 0 (VOID/BOOL) and STRING are variable/absent and are excluded."""
        assert DataType.VOID not in DATA_TYPE_STRUCT_FORMATS
        assert DataType.STRING not in DATA_TYPE_STRUCT_FORMATS

        for data_type in (
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
