# _statement_novelty
# _entity_novelty
# _negation_conflicts
# _complement_conflict
# _subject_gaps
# _complement_gaps
# _overlaps
# _trust

class Instruct():

    def __init__(self, language="English"):
        # type: () -> None
        """
        Generate natural language based on structured data

        Parameters
        ----------
        """
        self._language = language

    def get_instruct_for_statement (self):
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you as input: a phrase, followed by a perspective, followed by \"that\" and a triple with a subject, predicate and object.\
         You need to paraphrase the input in plain " + self._language + ". \
         Only reply with the short paraphrase of the input and only use the subject and object from the triple in your reply as given. \
         When responding use the names from the triple and be specific. \
         Do not give an explanation. \
         Do not explain what the subject and object is. \
         The response should be just the paraphrased text and nothing else."
        }
        return instruct

    def get_instruct_for_answer (self):
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you as input: the question, followed by the answer.\
         You need to paraphrase the input in plain " + self._language + ". \
         Only reply with the short paraphrase of the input and only use the subject and object from the triple in your reply as given. \
         When responding use the names from the triple and be specific. \
         Do not give an explanation. \
         Do not explain what the subject and object is. \
         The response should be just the paraphrased text and nothing else."
        }
        return instruct

    def get_instruct_for_no_answer (self):
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         You were given a question but you do not have an answer from your memory.\
         You are given the question as input and need to explain that you have no answer in plain " + self._language + ". \
         Only reply with the short paraphrase of the input and only use the subject and object from the triple in your reply as given. \
         When responding use the names from the triple and be specific. \
         Do not give an explanation. \
         The response should be just the paraphrased text and nothing else."
        }
        return instruct


    def get_instruct_for_subject_gap (self):
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you as input: a triple with a subject, a predicate and a type of subject.\
         You need to paraphrase the input in plain " + self._language + " as a question for the object. \
         Use who for the type person, where for the type location, when for the type time and what for everything else. \
         Only reply with the short paraphrase of the input. \
         When responding use the names from the triple and be specific. \
         Do not give an explanation. \
         Do not explain what the subject and object is. \
         Do not use the words agent, patient or experiencer in the paraphrase. \
         The response should be just the paraphrased text and nothing else."
               }
        return instruct


    def get_instruct_for_subject_gap_with_examples(self):
        """Same as get_instruct_for_subject_gap(), but the input also carries example values
        peers have for the missing predicate (see kg_gap_finder.py's `peer_examples` and
        prompts.response_processor._format_peer_examples()) -- used so the resulting question
        can suggest concrete, evidence-backed options instead of asking blind."""
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you as input: a triple with a subject, a predicate and a type of subject, \
         followed by example values that similar peers reported for that predicate. \
         You need to paraphrase the triple in plain " + self._language + " as a question for the object, \
         and naturally weave in one or two of the example values as suggestions to make the question \
         concrete and easy to answer -- without assuming or stating that they apply to the subject. \
         Use who for the type person, where for the type location, when for the type time and what for everything else. \
         Only reply with the short paraphrase of the input. \
         When responding use the names or labels from the triple and be specific. \
         Do not give an explanation. \
         Do not explain what the subject and object is. \
         Do not use the words agent, patient or experiencer in the paraphrase. \
         The response should be just the paraphrased text and nothing else."
               }
        return instruct


    def get_instruct_for_agent_confirmation(self):
        """Used by prompts.response_processor.get_prompt_for_agent_gap() for a gap on the
        `agent`/`agent_patient` role: instead of asking who did something (get_instruct_for_subject_gap()
        with an open "who" question), ask the named person to confirm it was them -- this is a
        single-human chat, so the activity's agent is essentially always the person speaking."""
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you as input: the name of a person, the word \"agent\", and an activity. \
         You need to phrase this in plain " + self._language + " as a short yes/no question, \
         addressed to that person, confirming that they themselves were the one doing the activity \
         -- e.g. \"Just to confirm, was that you who went for a run?\". \
         Only reply with the short confirmation question. \
         Do not give an explanation. \
         Do not explain what the input is. \
         The response should be just the question and nothing else."
               }
        return instruct


    def get_instruct_for_confirmation_response(self):
        """Used by prompts.response_processor.get_prompt_for_confirmation_response() to
        classify a human's reply to a get_instruct_for_agent_confirmation() yes/no question --
        see chat_sessions.KgChatSession._classify_confirmation_reply(), which parses the exact
        three-way output contract described here."""
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you a yes/no confirmation question that was asked, and the person's reply \
         to it. Classify the reply as exactly one of: \
         CONFIRM -- the person confirmed the statement is correct (e.g. yes, that's right, correct). \
         DENY -- the person denied it and did NOT give the correct information instead. \
         CORRECT: <value> -- the person denied it AND gave the correct information in the same \
         reply; <value> is that corrected information, stated as briefly and literally as \
         possible in a few words, e.g. \"CORRECT: my son\". \
         Respond with exactly one line, exactly one of these three forms, and nothing else -- \
         no explanation, no extra punctuation."
               }
        return instruct


    def get_instruct_for_gap_filled_ack(self):
        """Used by prompts.response_processor.get_prompt_for_gap_filled_ack() once a gap has
        just been filled (confirmed or corrected) and pushed to the KG -- a brief acknowledgement
        instead of another question."""
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you a short fact that the person just confirmed or corrected. \
         Reply with a brief, warm acknowledgement in plain " + self._language + ", at most one \
         short sentence (e.g. \"Got it, thanks!\"). Do not repeat the fact back in full. \
         Do not ask a new question. \
         The response should be just the acknowledgement and nothing else."
               }
        return instruct


    def get_instruct_for_object_gap (self):
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you as input: a triple with a subject, a predicate and a type of object.\
         You need to paraphrase the input in plain  " + self._language + "  as a question for the object. \
         Use who for the type person, where for the type location, when for the type time and what for everything else. \
         Only reply with the short paraphrase of the input. \
         When responding use the names from the triple and be specific. \
         Do not give an explanation. \
         Do not explain what the subject and object is. \
         The response should be just the paraphrased text and nothing else."
               }
        return instruct

    def get_instruct_for_novelty (self):
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you as input: a triple with a subject, a predicate and a type of object and a phrase.\
         You need to paraphrase the input in plain  " + self._language + ". \
         Only reply with the short paraphrase of the input. \
         When responding use the names from the triple and be specific. \
         Do not give an explanation. \
         Do not explain what the subject and object is. \
         The response should be just the paraphrased text and nothing else."
               }
        return instruct

    def get_instruct_for_conflict (self):
        instruct = {"role": "system", "content": "You are an intelligent assistant. \
         I will give you as input: a triple with a subject, a predicate and a type of object and a phrase that expresses a conflict.\
         You need to paraphrase the input in plain  " + self._language + ". \
         Only reply with the short paraphrase of the input. \
         When responding use the names from the triple and be specific. \
         Do not give an explanation. \
         Do not explain what the subject and object is. \
         The response should be just the paraphrased text and nothing else."
           }
        return instruct
