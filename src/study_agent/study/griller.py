from typing import Optional


class SocraticGriller:
    """
    Orchestrates interactive Socratic grilling sessions following Professor White's
    adversarial active-recall progression.
    """

    GRILL_SYSTEM_PROMPT = (
        "You are Professor White, an intellectually demanding, precise, and adversarial "
        "Socratic examiner. Your goal is to test the student's deep conceptual understanding, "
        "not mere rote memorization. Enforce German academic rigor: reject vague, hand-wavy answers.\n\n"
        "Progression Rules:\n"
        "- Level 1: Recall & Core Intuition (State definitions, invariants, or core theorems)\n"
        "- Level 2: Application & Tracing (Given concrete inputs, trace or calculate the behavior)\n"
        "- Level 3: Adversarial Edge Case (Professor White Special — edge cases, performance traps, or counterexamples)\n\n"
        "Directives:\n"
        "1. Ask exactly ONE crisp, challenging question at a time.\n"
        "2. Do NOT give away the answer or provide multi-part unsolicited hints.\n"
        "3. When the student answers, point out precisely what is right and what is flawed, then push to the next step."
    )

    @staticmethod
    def create_initial_prompt(topic: str, context_material: Optional[str] = None) -> str:
        """Generates the prompt to launch an interactive grilling session on a topic."""
        prompt = f"Let's grill the student on the topic: '{topic}'.\n"
        if context_material:
            prompt += f"\nRelevant study context:\n```\n{context_material}\n```\n"
        prompt += (
            "Start the grilling session now. Formulate the Level 1 question. "
            "Address the student directly with no polite filler."
        )
        return prompt

    @staticmethod
    def create_evaluation_prompt(
        topic: str,
        question: str,
        student_answer: str,
        reference_notes: Optional[str] = None
    ) -> str:
        """Evaluates student's answer and determines the next question."""
        prompt = (
            f"Topic: {topic}\n"
            f"Exam Question: {question}\n"
            f"Student Answer: {student_answer}\n"
        )
        if reference_notes:
            prompt += f"\nReference Source Notes:\n```\n{reference_notes}\n```\n"

        prompt += (
            "\nEvaluate the student's response with precision:\n"
            "1. Verdict: [Correct / Partially Correct / Incorrect]\n"
            "2. Precision Critique: Highlight exact misconceptions or missing nuances.\n"
            "3. Next Action: If correct, advance to the next level question. If flawed, ask a probing Socratic question to guide them."
        )
        return prompt
