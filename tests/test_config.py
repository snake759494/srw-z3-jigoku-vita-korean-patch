from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from siok_patch.config import ProjectConfig


class ProjectConfigTest(unittest.TestCase):
    def test_cpk_도구_경로를_읽고_다시_저장할_때_보존한다(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "archive"
            cpk_tool = root / "cpkmakec.exe"

            config = ProjectConfig.from_mapping(
                {
                    "archiveRoot": str(archive),
                    "cpkToolPath": str(cpk_tool),
                }
            )

            self.assertEqual(config.cpk_tool_path, cpk_tool.resolve())
            self.assertEqual(config.as_json()["cpkToolPath"], str(cpk_tool.resolve()))


if __name__ == "__main__":
    unittest.main()
