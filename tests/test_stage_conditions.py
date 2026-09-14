import unittest

from scripts.extract_stage_conditions import _condition_types


class StageConditionTypeTest(unittest.TestCase):
    def test_all_ids_on_a_condition_line_are_classified(self) -> None:
        block = """
        id_tbl = {
          0,-1,-1, -- 勝利条件
          1,2,3,   -- 敗北条件
          4,       -- ＳＲ条件
        };
        """
        self.assertEqual(
            _condition_types(block),
            {0: ["win"], 1: ["defeat"], 2: ["defeat"], 3: ["defeat"], 4: ["sr"]},
        )


if __name__ == "__main__":
    unittest.main()
