# nng

High-performance Python bindings for [nng](https://nng.nanomsg.org/) (nanomsg-next-gen), written in Cython.

Nanomsg-next-gen (NNG) is a high-performance messaging library written in C, which provides a simple and efficient API for building distributed applications. It supports various communication patterns, such as request-reply, publish-subscribe, and pipeline, and offers features like message framing, protocol handling, and transport abstraction.

# Why yet another Python wrapper for NNG?

I am aware of two already existing Python wrappers for NNG.

I have started developing python-nng because after having tested ZeroMQ for a project, I wanted a library with similar concepts but with less user boilerplate. NNG seemed a good fit, but the existing Python wrappers didn't seem as mature as the Python bindings for ZeroMQ. I wanted complete docstrings and examples, benchmarks to inform decisions, and complete error handling in the codebase. In addition, during my experimentation I found out that async methods for ZeroMQ had a significant overhead, and that I could reduce it with a custom Selector using zmq_poll. NNG being callback-based for completions, I hoped for a better asyncio integration and less async overhead. To achieve this level of integration, Cython is needed, which the other wrappers didn't utilize.

# Performance

See [PERFORMANCE.md](PERFORMANCE.md) for detailed benchmarks and interpretation.

# License

python-nng is licensed under the MIT License. See [LICENSE](LICENSE) for more details.

# On the use of AI

Much of the writing has been assisted by AI (Claude Sonnet 4.6), in particular the benchmark code and the unit tests. I have 12 years of Python experience, and more in C/C++. I have written another Cython-based library [DearCyGui](https://github.com/DearCyGui/DearCyGui). Thus while AI has been a great help, I have extensively reviewed, extended, and modified the code and documentation, and I am confident that the code is of good quality. If you find any issue, please open an issue or a PR. 