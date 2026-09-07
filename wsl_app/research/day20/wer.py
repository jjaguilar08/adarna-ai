"""
Word-error-rate helper shared by both Track A and Track B benchmark scripts.
Standard Levenshtein word-level WER (substitutions + deletions + insertions,
divided by reference word count) -- the same metric speech-recognition
benchmarks conventionally report, so numbers here are comparable to
published STT accuracy claims (including WhisperX's own paper).
"""
import re


def normalize_words(text):
    """
    Lowercases and strips punctuation so wording differences (case,
    trailing commas/periods) don't get counted as word errors -- WER should
    reflect real transcription mistakes, not formatting.

    Returns:
        list[str]: the text's words, normalized.
    """
    text = text.lower()
    text = re.sub(r"[^\w\s']", " ", text)
    return text.split()


def word_error_rate(reference_text, hypothesis_text):
    """
    Computes word error rate between a reference (ground truth) and a
    hypothesis (transcribed) text via Levenshtein edit distance at the
    word level.

    Returns:
        tuple[float, int, int]: (WER as a fraction, edit distance, reference
        word count). WER can exceed 1.0 if the hypothesis has many more
        insertions than the reference has words.
    """
    ref = normalize_words(reference_text)
    hyp = normalize_words(hypothesis_text)
    rows, cols = len(ref) + 1, len(hyp) + 1
    distance = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        distance[i][0] = i
    for j in range(cols):
        distance[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            if ref[i - 1] == hyp[j - 1]:
                distance[i][j] = distance[i - 1][j - 1]
            else:
                distance[i][j] = 1 + min(
                    distance[i - 1][j],      # deletion
                    distance[i][j - 1],      # insertion
                    distance[i - 1][j - 1],  # substitution
                )
    edits = distance[-1][-1]
    ref_len = max(len(ref), 1)
    return edits / ref_len, edits, len(ref)
