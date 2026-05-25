coverage:
	pytest --cov=research_assistant.screeners --cov=research_assistant.journal --cov-report=term --cov-fail-under=80
