# Contributing to litellm-multi-gateway

First off, thank you for considering contributing! 🎉

This project aims to be a practical, no-nonsense gateway for multi-backend LLM routing. We welcome contributions of all kinds.

## Ways to Contribute

- 🐛 **Report bugs** — Open an issue with reproduction steps
- 💡 **Suggest features** — Tell us what backends/providers you'd like supported
- 🔧 **Submit PRs** — Bug fixes, new providers, vision hook improvements
- ⭐ **Star the repo** — Helps others discover it
- 📝 **Improve docs** — Fix typos, add examples, translate

## Adding a New Backend Provider

1. Add the provider to `litellm/profiles/multi.yaml` under `model_list`
2. Mark `# needs_vision: true/false` based on whether the provider supports images
3. Add the API key variable to `.env.example`
4. Update `keys.sh` if the provider needs special alias handling
5. Test with `docker compose up -d` and verify via Admin UI

## Development Setup

```bash
git clone https://github.com/kwhitestone/litellm-multi-gateway.git
cd litellm-multi-gateway
cp .env.example .env
# Fill in at least one provider key

docker compose up -d
docker compose logs -f litellm   # Watch logs
```

## PR Checklist

- [ ] Code follows existing style
- [ ] No API keys or secrets committed
- [ ] `.env.example` updated if new env vars added
- [ ] README updated if behavior changed
- [ ] Tested with `docker compose up -d`

## Reporting Security Issues

**Do not open a public issue for security vulnerabilities.** Instead, please email the maintainer directly or open a private security advisory on GitHub.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
