Server that manages execution of arbitrary Python. This server itself doesn't actually
execute any user-defined python. It instead relies on the `executor`.

To build locally:

```
docker build . -t code-interpreter
```
