# vera-challenge
Vera AI Bot — A FastAPI-based intelligent engagement bot built for the Magicpin Vera AI Challenge, supporting context management, trigger processing, conversational replies, consent handling, opt-outs, and automated customer/merchant engagement.
# Vera AI Bot 🤖

An intelligent, production-ready engagement bot built for the **Magicpin Vera AI Challenge**.

The bot is implemented using **Python and FastAPI** and provides REST APIs for managing context, processing triggers, and generating conversational responses for merchants and customers.

## 🚀 Features

- Context management with version control
- Merchant and customer engagement
- Trigger-based conversations
- Personalized responses
- Customer consent handling
- Opt-out and STOP handling
- Automatic reply detection
- Positive-intent detection
- Duplicate message prevention
- Conversation state management
- Health and metadata endpoints
- Docker and Render deployment support

## 🔌 API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/v1/context` | Load or update bot context |
| POST | `/v1/tick` | Process engagement triggers |
| POST | `/v1/reply` | Generate conversational replies |
| GET | `/v1/healthz` | Health check |
| GET | `/v1/metadata` | Bot metadata |
| POST | `/v1/teardown` | Clear bot state |

## 🛠️ Tech Stack

- Python
- FastAPI
- Pydantic
- Uvicorn
- REST API
- Docker
- Render

## 🎯 Challenge Focus

The bot is designed to provide safe, contextual, and personalized engagement while respecting:

- Customer consent
- Opt-out requests
- Conversation state
- Duplicate suppression
- Trigger-specific business context
- Clear calls-to-action

## ▶️ Run Locally

```bash
pip install -r requirements.txt
uvicorn app:app --reload
