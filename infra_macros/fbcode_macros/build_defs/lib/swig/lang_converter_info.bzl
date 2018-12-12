"""
LangConverterInfo defines a contract for all swig lang converters.

The methods that are required by LangConverterInfo have following signatures:

def get_lang():
    # Return the language name.

def get_lang_opt():
    # Return the language flag to pass into swig.

def get_lang_flags(**kwargs):
    # Return language specific flags to pass to swig.

def get_generated_sources(module):
    # Return the language-specific sources generated by swig.

def get_language_rule(
        base_path,
        name,
        module,
        hdr,
        src,
        gen_srcs,
        cpp_deps,
        deps,
        visibility,
        **kwargs):
    # Generate the language-specific library rule (and any extra necessary
    # rules).
"""

LangConverterInfo = provider(fields = [
    "get_lang",
    "get_lang_opt",
    "get_lang_flags",
    "get_generated_sources",
    "get_language_rule",
])
