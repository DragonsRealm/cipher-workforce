"""Contract for the Gallery listing surface.

The path shape is the thing under test. ``WorkspaceBackend._file_info``
emits a leading slash; every consumer of a file reference wants it relative;
and the difference only fails on POSIX, because
``Path('/audio/x.wav').is_absolute()`` is True there and False on Windows.
A leading slash would therefore drag fine on a Windows dev box and break in
production, so the round-trip assertion below is the regression test that
matters most here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nodes.filesystem.gallery._service import (
    WORKSPACE_LIST_LIMIT,
    list_directory,
    list_matching,
    to_file_ref,
)

pytestmark = pytest.mark.unit

WORKFLOW_ID = "019f99e2dc997cf390193ad3e6260de1"


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "audio").mkdir()
    (tmp_path / "downloads").mkdir()
    (tmp_path / "audio" / "greeting.wav").write_bytes(b"RIFFDATA")
    (tmp_path / "chart.png").write_bytes(b"PNGDATA")
    (tmp_path / "notes.txt").write_text("hello")
    return tmp_path


class TestPathShape:
    async def test_paths_are_relative_with_no_leading_slash(self, workspace):
        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)

        for entry in result["entries"]:
            assert not entry["path"].startswith("/"), entry
            assert not entry["path"].endswith("/"), entry
            assert "\\" not in entry["path"], entry
            assert not Path(entry["path"]).is_absolute()

    async def test_a_listed_path_resolves_back_inside_the_workspace(self, workspace):
        """The regression test for the POSIX-only failure.

        A leading-slash path takes resolve_media's absolute branch on Linux,
        fails relative_to(root), and raises "outside this workflow's
        workspace" -- while working on Windows.
        """
        from services.media.workspace import read_media_bytes, resolve_media

        result = await list_directory(
            str(workspace), path="audio", workflow_id=WORKFLOW_ID
        )
        row = result["entries"][0]

        resolved = resolve_media(row["path"], workspace_dir=str(workspace))
        assert resolved.relative_to(workspace)
        assert read_media_bytes(row["path"], workspace_dir=str(workspace))[1] == b"RIFFDATA"

    async def test_a_listed_row_lifts_into_a_readable_file_ref(self, workspace):
        """What a drag onto a file parameter actually carries."""
        from services.media.refs import FileRef
        from services.media.workspace import coerce_file_param
        from types import SimpleNamespace

        result = await list_directory(
            str(workspace), path="audio", workflow_id=WORKFLOW_ID
        )
        ref = to_file_ref(result["entries"][0], WORKFLOW_ID)

        assert FileRef.model_validate(ref)
        ctx = SimpleNamespace(
            workspace_dir=str(workspace), node_id="n", workflow_id=WORKFLOW_ID
        )
        assert coerce_file_param(ref, ctx=ctx)[1] == b"RIFFDATA"

    async def test_listing_never_claims_a_richer_kind_than_it_probed(self, workspace):
        """A .wav listed is kind='file', not 'audio'.

        kind='audio' asserts inspect_audio ran; claiming it with a null
        duration would mis-bill a per-second provider downstream.
        """
        result = await list_directory(
            str(workspace), path="audio", workflow_id=WORKFLOW_ID
        )
        ref = to_file_ref(result["entries"][0], WORKFLOW_ID)

        assert ref["kind"] == "file"
        assert ref["filename"].endswith(".wav")


class TestListingShape:
    async def test_directories_sort_before_files(self, workspace):
        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)
        kinds = [entry["is_dir"] for entry in result["entries"]]

        assert kinds == sorted(kinds, reverse=True)

    async def test_urls_are_path_only_and_absent_for_directories(self, workspace):
        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)

        for entry in result["entries"]:
            if entry["is_dir"]:
                assert entry["url"] is None
            else:
                assert entry["url"].startswith(f"/api/workspace/{WORKFLOW_ID}/files/")
                assert "://" not in entry["url"]

    async def test_parent_is_empty_at_root_and_set_below(self, workspace):
        assert (await list_directory(str(workspace)))["parent"] is None
        assert (await list_directory(str(workspace), path="audio"))["parent"] == ""

    async def test_missing_directory_is_distinguishable_from_an_empty_one(
        self, workspace
    ):
        """ls_info returns [] for both; the panel must tell them apart."""
        empty = await list_directory(str(workspace), path="downloads")
        assert empty["entries"] == [] and empty["path_exists"] is True

        gone = await list_directory(str(workspace), path="nope")
        assert gone["entries"] == [] and gone["path_exists"] is False

    async def test_absent_workspace_reports_itself(self, tmp_path):
        result = await list_directory(str(tmp_path / "never-created"))

        assert result["workspace_exists"] is False
        assert result["entries"] == []


class TestCaps:
    async def test_cap_truncates_and_reports(self, tmp_path):
        for index in range(30):
            (tmp_path / f"file-{index:03d}.txt").write_text("x")

        result = await list_directory(str(tmp_path), limit=10)

        assert result["count"] == 10
        assert result["truncated"] is True

    async def test_directories_survive_truncation(self, tmp_path):
        """Why dirs sort first: the cap must never strip navigation."""
        (tmp_path / "zzz_folder").mkdir()
        for index in range(30):
            (tmp_path / f"aaa-{index:03d}.txt").write_text("x")

        result = await list_directory(str(tmp_path), limit=5)

        assert any(entry["is_dir"] for entry in result["entries"])

    async def test_limit_is_clamped_to_the_hard_cap(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")

        result = await list_directory(str(tmp_path), limit=10_000_000)

        assert result["count"] <= WORKSPACE_LIST_LIMIT


class TestTraversal:
    @pytest.mark.parametrize("attack", ["../..", "../../etc", "~", "..\\.."])
    async def test_traversal_is_refused(self, workspace, attack):
        from services.plugin import NodeUserError

        (workspace.parent / "secret.txt").write_text("SECRET")

        with pytest.raises(NodeUserError):
            await list_directory(str(workspace), path=attack)

    @pytest.mark.parametrize("rooted", ["/etc", "C:/Windows", "/"])
    async def test_rooted_paths_are_reinterpreted_inside_the_workspace(
        self, workspace, rooted
    ):
        """These are not escapes and must not be refused.

        A leading slash is the *virtual* root (the workspace itself), and a
        drive prefix is stripped by _validate_virtual_path. '/etc' therefore
        means '<workspace>/etc'. The property under test is containment, not
        which mechanism enforces it.
        """
        from nodes.filesystem._backend import normalize_virtual_path, resolve_within

        resolved = resolve_within(workspace, normalize_virtual_path(rooted))
        assert resolved.is_relative_to(workspace)

        # And listing it simply finds nothing, rather than reading the host.
        result = await list_directory(str(workspace), path=rooted)
        assert result["entries"] == [] or all(
            not entry["path"].startswith("..") for entry in result["entries"]
        )

    async def test_glob_pattern_cannot_escape(self, workspace):
        from services.plugin import NodeUserError

        with pytest.raises((NodeUserError, ValueError)):
            await list_matching(str(workspace), pattern="../*", path="")


class TestGlob:
    async def test_matches_recursively_and_returns_files_only(self, workspace):
        result = await list_matching(
            str(workspace), pattern="*.wav", workflow_id=WORKFLOW_ID
        )

        assert result["count"] == 1
        assert result["entries"][0]["path"] == "audio/greeting.wav"
        assert all(not entry["is_dir"] for entry in result["entries"])


class TestNodeExecution:
    async def test_emits_file_refs_for_downstream_nodes(self, workspace):
        from tests.nodes._harness import NodeTestHarness  # noqa: F401  (availability)
        from nodes.filesystem.gallery import GalleryNode, GalleryParams
        from types import SimpleNamespace

        ctx = SimpleNamespace(
            workspace_dir=str(workspace),
            workflow_id=WORKFLOW_ID,
            node_id="fm-1",
            raw={"workspace_dir": str(workspace)},
        )
        result = await GalleryNode().list_files(
            ctx, GalleryParams(path="audio")
        )

        assert result.count == 1
        assert result.files[0]["kind"] == "file"
        assert result.files[0]["path"] == "audio/greeting.wav"

    async def test_selection_reports_missing_without_failing(self, workspace):
        from nodes.filesystem.gallery import GalleryNode, GalleryParams
        from types import SimpleNamespace

        ctx = SimpleNamespace(
            workspace_dir=str(workspace),
            workflow_id=WORKFLOW_ID,
            node_id="fm-1",
            raw={"workspace_dir": str(workspace)},
        )
        result = await GalleryNode().list_files(
            ctx,
            GalleryParams(selection=["audio/greeting.wav", "audio/gone.wav"]),
        )

        assert result.count == 1
        assert result.missing == ["audio/gone.wav"]

    async def test_selection_that_is_entirely_missing_is_a_user_error(self, workspace):
        from nodes.filesystem.gallery import GalleryNode, GalleryParams
        from services.plugin import NodeUserError
        from types import SimpleNamespace

        ctx = SimpleNamespace(
            workspace_dir=str(workspace),
            workflow_id=WORKFLOW_ID,
            node_id="fm-1",
            raw={"workspace_dir": str(workspace)},
        )
        with pytest.raises(NodeUserError):
            await GalleryNode().list_files(
                ctx, GalleryParams(selection=["nope.wav"])
            )

    async def test_output_stays_well_under_the_temporal_warning_budget(self, tmp_path):
        """1000 FileRef rows must not approach the payload ceiling."""
        import orjson

        from nodes.filesystem.gallery import GalleryNode, GalleryParams
        from services.media.limits import TEMPORAL_PAYLOAD_ERROR_BYTES
        from types import SimpleNamespace

        for index in range(1000):
            (tmp_path / f"generated-file-name-{index:04d}.wav").write_bytes(b"x")

        ctx = SimpleNamespace(
            workspace_dir=str(tmp_path),
            workflow_id=WORKFLOW_ID,
            node_id="fm-1",
            raw={"workspace_dir": str(tmp_path)},
        )
        result = await GalleryNode().list_files(ctx, GalleryParams(limit=1000))

        size = len(orjson.dumps(result.model_dump(mode="json")))
        assert size < TEMPORAL_PAYLOAD_ERROR_BYTES


class TestWorkspaceContainmentOfSymlinks:
    @pytest.mark.skipif(
        not hasattr(__import__("os"), "symlink"), reason="no symlink support"
    )
    async def test_a_symlink_escaping_the_workspace_is_not_listed(
        self, workspace, tmp_path
    ):
        import os

        outside = tmp_path.parent / "outside-secret.txt"
        outside.write_text("SECRET")
        try:
            os.symlink(outside, workspace / "escape.txt")
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this platform")

        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)

        assert "escape.txt" not in [entry["name"] for entry in result["entries"]]


class TestRowsAreSelfSufficient:
    """The panel renders; it must not have to re-derive anything.

    Both fields here exist to delete a copy of a server rule that had been
    living in TypeScript: one hand-assembling a Pydantic model that forbids
    unknown fields, the other second-guessing the route's disposition.
    """

    async def test_every_file_row_carries_a_finished_reference(self, workspace):
        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)

        for entry in result["entries"]:
            if entry["is_dir"]:
                # There is no FileRef to a directory; null is what stops a
                # client offering to drag one somewhere.
                assert entry["ref"] is None
            else:
                assert entry["ref"] == to_file_ref(entry, WORKFLOW_ID)

    async def test_the_carried_reference_validates_as_a_FileRef(self, workspace):
        from services.media.refs import FileRef

        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)
        rows = [entry for entry in result["entries"] if not entry["is_dir"]]
        assert rows

        for entry in rows:
            # extra="forbid", so this fails loudly if the row ever grows a
            # field the model does not know about.
            assert FileRef.model_validate(entry["ref"]).path == entry["path"]

    async def test_preview_matches_what_the_route_will_serve_inline(self, workspace):
        from services.media.preview import serves_inline

        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)

        for entry in result["entries"]:
            previewable = entry["preview"] != "none"
            assert previewable is (
                not entry["is_dir"] and serves_inline(entry["mime_type"])
            )

    async def test_media_is_previewable_and_text_is_not(self, workspace):
        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)
        by_name = {entry["name"]: entry for entry in result["entries"]}

        assert by_name["chart.png"]["preview"] == "image"
        assert by_name["notes.txt"]["preview"] == "none"
        assert by_name["audio"]["preview"] == "none"

    async def test_an_svg_is_never_previewable(self, workspace):
        # Script-bearing, so the route forces `attachment`. A panel that
        # opened an <img> for it would show a dead frame after a download.
        (workspace / "diagram.svg").write_text("<svg/>")

        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)
        by_name = {entry["name"]: entry for entry in result["entries"]}

        assert by_name["diagram.svg"]["mime_type"] == "image/svg+xml"
        assert by_name["diagram.svg"]["preview"] == "none"


class TestBreadcrumbs:
    async def test_root_has_no_trail(self, workspace):
        result = await list_directory(str(workspace), workflow_id=WORKFLOW_ID)

        assert result["crumbs"] == []

    async def test_each_segment_carries_the_path_to_navigate_to(self, workspace):
        (workspace / "audio" / "clips").mkdir()

        result = await list_directory(
            str(workspace), path="audio/clips", workflow_id=WORKFLOW_ID
        )

        assert result["crumbs"] == [
            {"name": "audio", "path": "audio"},
            {"name": "clips", "path": "audio/clips"},
        ]


class TestSearchTermTranslation:
    """One definition of what "search" means, shared by panel and node."""

    def test_a_bare_word_becomes_a_containment_glob(self):
        from nodes.filesystem.gallery._service import search_to_pattern

        # Unwrapped, `greeting` matches nothing and the box looks broken.
        assert search_to_pattern("greeting") == "*greeting*"

    def test_an_explicit_glob_passes_through(self):
        from nodes.filesystem.gallery._service import search_to_pattern

        assert search_to_pattern("*.wav") == "*.wav"
        assert search_to_pattern("clip?.mp3") == "clip?.mp3"
        assert search_to_pattern("[abc]*.txt") == "[abc]*.txt"

    def test_whitespace_only_is_not_a_search(self):
        from nodes.filesystem.gallery._service import search_to_pattern

        assert search_to_pattern("   ") == ""
        assert search_to_pattern("") == ""

    async def test_a_search_finds_a_file_in_a_subdirectory(self, workspace):
        from nodes.filesystem.gallery._service import search_to_pattern

        result = await list_matching(
            str(workspace),
            pattern=search_to_pattern("greet"),
            workflow_id=WORKFLOW_ID,
        )

        assert [entry["path"] for entry in result["entries"]] == ["audio/greeting.wav"]
