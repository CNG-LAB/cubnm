from distutils.core import setup, Extension
import os

os.environ["CC"] = "g++"
os.environ["CXX"] = "g++"

"""
Pre-requisites:

GSL2.7
> download and unzip & cd to directory
> ./configure --prefix=<target_dir> --enable-shared
> make && make install

libks
> git clone ... && cd
> make
"""

bnm_ext = Extension(
    'bnm',
    ['run_CMAES.cpp'],
    language='c++',
    extra_compile_args=[
        "-O3",
        "-m64",
        "-fopenmp",
    ],
    libraries=["m", "gomp"],
    extra_objects=[
        "/data/project/ei_development/tools/gsl_build_shared/lib/libgsl.a",
        "/data/project/ei_development/tools/gsl_build_shared/lib/libgslcblas.a",
        "/data/project/ei_development/tools/libks/libks.so",
    ],
    include_dirs=[
        '/data/project/ei_development/tools/gsl_build/include', 
        '/data/project/ei_development/tools/libks/include'],
)

setup(name = 'bnm', version = '1.0',  \
   ext_modules = [bnm_ext])