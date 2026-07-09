mod decrypt;
mod media;
mod mp4;
mod mux;
mod python;

use pyo3::prelude::*;

#[pymodule]
fn _ammuxer(module: &Bound<'_, PyModule>) -> PyResult<()> {
    python::register(module)
}
